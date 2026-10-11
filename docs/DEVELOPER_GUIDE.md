# 開発者ガイド

この文書は、コード変更、検証、CLI実行、OS別アプリ生成を行う開発者向けです。初心者向けの通常導入・操作は[ユーザーガイド](USER_GUIDE.md)、不具合の切り分けは[トラブルシューティング](TROUBLESHOOTING.md)を参照してください。

公開前には[利用上の注意・規約](TERMS_AND_DISCLAIMER.md)と[配布ガイド](PACKAGING.md)も確認してください。ジョブ・動画・音声・設定等の実行時データはGit管理・ソース配布の対象にしません。既存データをGit管理から外しても過去コミットには残るため、機密・個人情報の公開可否は別途確認します。Git履歴の除去は共同作業への影響を確認し、管理者の承認なしに行わないでください。

## 目次

- [1. 対象範囲と構成](#1-対象範囲と構成)
- [2. 実行経路とジョブ管理](#2-実行経路とジョブ管理)
- [3. 開発環境](#3-開発環境)
- [4. CLIでパイプラインを実行する](#4-cliでパイプラインを実行する)
- [5. 設定・環境変数](#5-設定環境変数)
- [6. 成果物とクリーンアップ契約](#6-成果物とクリーンアップ契約)
- [7. テストと手動確認](#7-テストと手動確認)
- [8. ビルド・更新・配布](#8-ビルド更新配布)
- [9. 保守上の注意と既知の制限](#9-保守上の注意と既知の制限)

## 1. 対象範囲と構成

現行のアプリはローカルGUIとローカル子プロセスの構成です。FastAPI、SSH、SCP、Slurmを使った旧構成とは別です。GUIの対応対象はmacOS（arm64／x86_64）とWindows 11 x64です。パイプラインにはLinux実行経路もありますが、Linuxの初心者向けGUI導入・配布は本マニュアルの対象外です。

Pythonは標準セットアップで3.10～3.13を選択します。依存関係の都合で3.14を対応済みとは扱いません。依存バージョンの詳細は [requirements.txt](../source/worker/requirements.txt) が正本です。

```text
ondemand_translater/
├── docs/                            OS別導入・操作・調査・開発マニュアル
├── packaging/                       同梱版のビルド・版番号・配布定義
├── .github/workflows/               OS別ビルドとドラフトRelease生成
├── mac/
│   ├── install.command / install.sh macOS依存環境の導入
│   ├── build_app.sh                 Objective-Cランチャーの生成
│   └── TranslateServiceLauncher.m   実行位置、PATH、データ場所、起動ログ設定
├── win/
│   ├── install.cmd / install.ps1    Windows依存環境とショートカットの導入
│   ├── build_exe.ps1                PyInstallerビルド
│   ├── python_environment.ps1       Python検出・仮想環境検証・導入結果確認
│   ├── tests/                      PowerShellの環境検出・復旧テスト
│   └── run_standalone.ps1           GUI・変換Pythonの環境設定
└── source/worker/
    ├── standalone_app.py            Tkinter GUI、設定・用語CSV
    ├── app_runtime.py               同梱判定・資源・worker・FFmpegの解決
    ├── bundled_worker.py            同梱変換workerの入口
    ├── credential_store.py          Keychain／Credential Manager
    ├── local_job_manager.py         SQLite、FIFO、子プロセス、進捗、成果物転送
    ├── run_standalone.sh            macOS等のPython起動
    ├── requirements.txt             標準依存
    ├── tests/test_local_job_manager.py
    ├── lecture_poc/
    │   ├── __main__.py              CLI引数とエラー表示
    │   ├── pipeline.py              変換・翻訳・音声生成・結合
    │   ├── jp_cleaner.py            日本語字幕整形
    │   ├── srt_utils.py             SRTの解析・整形
    │   ├── video_text_overlay.py    任意のOCR・映像テキスト置換
    │   ├── security.py              出力エラーの秘密マスキング
    │   └── prompts/                既定用語、Whisper・整形プロンプト
    └── bin/                         旧管理・整理ツール（使用前に実装確認）
```

## 2. 実行経路とジョブ管理

### OS別起動

| 入口 | 経路・注意 |
|---|---|
| 同梱版（両OS） | 凍結GUI → 隣接する `TranslateWorker`／`TranslateWorker.exe`。Python・FFmpegも同梱 |
| ソース版Mac Finder | `.app` → Objective-Cランチャー → `run_standalone.sh` → `.venv` のPython |
| Macターミナル | `run_standalone.sh` → `.venv` または `TRANSLATE_PYTHON` |
| Windowsショートカット | PowerShell → `run_standalone.ps1` → 生成済みexe。exeがない場合はPython GUI |
| ソース版GUIから変換 | `LocalJobManager` → Pythonの `-m lecture_poc` |

ソース版Windowsランナーは `TRANSLATE_WORKER_PYTHON` を `.venv\Scripts\python.exe` に、`PYTHONPATH` をworkerディレクトリに設定します。ソース版のexeだけをコピーしても動作しません。

同梱版は凍結状態と資源の `BUNDLED` マーカーで識別し、旧Windowsビルドと区別します。workerは独立したPyInstallerプロセスとして起動し、外部Pythonの指定を除去します。プロンプトと `VERSION` は同梱資源から読みます。Piperは同梱モデルを使い、システムのTTSコマンドに依存しません。両実行ファイルの `--bundle-self-test [report.json]` は画面・モデル取得・外部API通信なしで資源と依存を検査し、workerは短いPiper音声を実際に生成します。

### ジョブのライフサイクル

1. `register_job()` はMP4と任意SRTの存在・拡張子を検証します。
2. UUIDのジョブディレクトリを作成し、動画を `input.mp4`、字幕を `jp.srt`／`en.srt` にコピーします。
3. 英語SRTだけの場合は空の `jp.srt` も作成し、不要な文字起こしを省略します。
4. 登録時の設定をJSONへ保存し、SQLiteに `queued` を登録します。
5. マネージャーのスレッドがSQLiteの `created_at` 昇順で1件ずつ処理します。
6. JSONを `queue/` から `processing/` へ移動し、`running` として子プロセスを起動します。
7. 子プロセスの出力を `logs/<job_id>.log` に保存し、`pipeline.log` の工程から進捗を更新します。
8. 正常終了後、動画候補を `output.mp4` へ正規化し、指定先へ動画と存在する出力字幕をコピーします。
9. ジョブ内の不要動画を削除し、JSONを `done/` へ移動して `done`／100%を保存します。
10. 例外時は `error` とエラー内容を保存し、JSONを `error/` へ移します。キャンセルも管理JSONは `error/` に配置されます。

SQLiteの状態は `queued`、`running`、`done`、`error`、`cancelled`。GUI一覧は登録日時の降順です。完了ジョブもDBに保持します。旧workerの「完了後DBから削除」の説明は適用しません。

起動時には `processing/` のジョブを調べ、DBが `running` なら `queued` へ戻す復旧処理があります。稼働中の別インスタンスを区別する全体ロックや完全なクラッシュリカバリーではありません。

`cancel()`／`stop()` は実行中の親プロセスへ `terminate()` を送ります。子プロセスツリー全体の停止や、任意の工程からの自動再開を保証しません。GUIを閉じた後の処理継続を機能として案内しないでください。

### 進捗

工程の固定重みと `START`／`DONE` ログから算出します。処理中は最大99%、エクスポートと動画整理後に100%になります。残り時間の推定ではありません。ログ形式や工程名を変更する場合は `PIPELINE_STAGES`、進捗テスト、ユーザー説明を同時に確認します。

## 3. 開発環境

### 推奨セットアップ

まず各OSの標準インストーラーで依存とTkを準備します。

- [macOSインストール手順](INSTALL_MAC.md)
- [Windowsインストール手順](INSTALL_WINDOWS.md)

仮想環境はOS・CPUアーキテクチャ間で共用しません。Windowsの更新版インストーラーは既存環境を実行して検証し、正常な対応版は再利用します。非対応・破損した環境は退避して再作成します。Macのインストーラーは既存環境を再利用するため、Python変更時に自動再作成されるとは限りません。

### WindowsのPython選択と仮想環境検証

[python_environment.ps1](../win/python_environment.ps1) を導入・ビルド・起動の各スクリプトから読み込みます。

- 新規環境の候補は、Launcherの明示バージョン指定、PEP 514のHKCU／HKLM／WOW6432Node登録、ユーザー・システム標準導入先、PATHの全候補から収集し重複を除去します。
- 3.12、3.13、3.11、3.10の優先順で、x64・対応バージョン・Tkinter／venv／ensurepipが使えるベースPythonを選びます。WindowsAppsの実行エイリアスや他の仮想環境はベース候補として使いません。
- 検証用Pythonは `-I` で起動し、ユーザーの `PYTHONPATH`／`PYTHONHOME` 等の混在を避けます。
- 正常な既存 `.venv` は再利用します。新しいベースへ切り替えたい場合は `install.ps1 -PythonPath` で絶対パスを指定します。指定が不正なら自動候補へフォールバックせず停止します。
- 仮想環境は対応バージョン・x64・Tkinter／venv／ensurepip／pipに加えて、`sys.prefix` が指定 `.venv` と一致し `sys.base_prefix` と異なることを確認します。
- 作成時は採用したベースとのバージョン・`base_prefix` の一致も確認します。
- 拒否した環境は `.venv.backup-日時-識別子` へ退避します。削除・自動復元は行いません。退避失敗時は作成へ進みません。
- ネイティブコマンドの終了コードを確認し、venv作成、pip導入、PyInstallerビルド、最終 `pip check` の失敗時に成功メッセージやショートカット作成へ進みません。

標準導入先・登録情報にないPythonは `-PythonPath` で指定します。PC全体のPATHや他のアプリ用のPythonは変更しません。FFmpegの検出・導入は別経路です。

### 実行環境の確認

以下のコマンドはリポジトリルートから実行します。

macOS：

```bash
source/worker/.venv/bin/python -c 'import sys, tkinter; print(sys.version); print(sys.executable)'
source/worker/.venv/bin/python -m pip check
```

Windows PowerShell：

```powershell
& .\source\worker\.venv\Scripts\python.exe -c "import sys, tkinter; print(sys.version); print(sys.executable)"
& .\source\worker\.venv\Scripts\python.exe -m pip check
```

ターミナルの `python` とアプリの `.venv` が同じとは限りません。Tkinterのチェックはライブラリ導入のチェックと分けます。GUIにはTkが必要ですが、CLIパイプラインの実行自体はTk画面を使いません。

### 非対応の既存環境を切り分ける

アプリと子処理を停止してから、Windowsでは更新版インストーラーの自動検証・退避を利用します。Macなどで手動切り分けする場合は `source/worker/.venv` だけを別名へ退避し、標準インストーラーで対応Pythonの環境を作り直します。退避した環境は診断用で、パス変更後にそのまま実行することは推奨しません。

ジョブ・出力・設定のフォルダーを削除する必要はありません。新しい環境の動作を確認するまで退避環境を残します。依存導入が原因か確認せず、無関係なライブラリを追加しないでください。

### 開発中のGUI起動

MacでFinderと同じ保存場所を使う場合（リポジトリルートから）：

```bash
export TRANSLATE_DATA_DIR="$PWD/source/worker/data"
export GEMINI_API_KEY="実際のキー"
bash source/worker/run_standalone.sh
```

Windowsで標準ランナーを使う場合：

```powershell
$env:GEMINI_API_KEY = "実際のキー"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\win\run_standalone.ps1
```

生成済みexeがあるとWindowsランナーはexeを優先するため、Python GUIの変更を直接確認する場合は：

```powershell
$env:TRANSLATE_WORKER_PYTHON = (Resolve-Path .\source\worker\.venv\Scripts\python.exe).Path
$env:PYTHONPATH = (Resolve-Path .\source\worker).Path
& .\source\worker\.venv\Scripts\python.exe .\source\worker\standalone_app.py
```

キーは環境変数を使い、コマンドラインの `--gemini-api-key` へ直接書く運用を避けます。履歴、プロセス一覧、ログ等への露出を減らすためです。

## 4. CLIでパイプラインを実行する

CLIはGUIのジョブ管理を経由しません。出力名と動画自動削除の動作が異なります。各実験は別の出力ディレクトリで実行してください。

### 基本実行

macOS（リポジトリルートから）：

```bash
export GEMINI_API_KEY="実際のキー"
cd source/worker
.venv/bin/python -m lecture_poc \
  --input "/実際の場所/lecture.mp4" \
  --outdir "/実際の場所/cli-output" \
  --translation-backend gemini \
  --transcription-preset balanced \
  --embed-subs --embed-subs-container mp4
```

Windows PowerShell（リポジトリルートから）：

```powershell
$env:GEMINI_API_KEY = "実際のキー"
cd source\worker
& .\.venv\Scripts\python.exe -m lecture_poc `
  --input "C:\実際の場所\lecture.mp4" `
  --outdir "C:\実際の場所\cli-output" `
  --translation-backend gemini `
  --transcription-preset balanced `
  --embed-subs --embed-subs-container mp4
```

上のパスはプレースホルダーです。実在するパスへ置き換えます。PowerShellの行継続記号 `` ` `` の後ろには空白を付けません。

### 正本の引数一覧

workerディレクトリで：

```bash
.venv/bin/python -m lecture_poc --help
```

Windowsでは `.venv\Scripts\python.exe` を使います。引数定義の正本は [__main__.py](../source/worker/lecture_poc/__main__.py) です。

| 引数 | 主な用途 |
|---|---|
| `--input`、`--outdir` | 必須。入力動画と生成先 |
| `--transcription-preset` | `fast`／`balanced`／`high-accuracy` |
| `--model`、`--whisper-prompt-file` | モデルサイズ・文字起こし用ヒントの手動指定 |
| `--transcription-device` | `cpu`／`cuda`／`auto`。CUDAには別途適切な実行環境が必要 |
| `--transcription-compute-type` | `int8` 等。デバイスとモデルに適合する値を指定 |
| `--chunk-seconds`、`--chunk-overlap-seconds` | 長尺文字起こしの分割・境界重複 |
| `--transcription-workers`、`--transcription-cpu-threads` | 並列数・CPU資源の調整 |
| `--transcription-max-pending-chunks` | 待機音声チャンクの上限 |
| `--lowpass-cutoff-hz` | 音声前処理。既定6500Hz、0以下で無効 |
| `--jp-clean-mode` | `off`／`rule`／`ollama`。GUIは `rule` |
| `--ollama-model` | ローカル整形モデル。Ollama本体とモデルの準備は別途必要 |
| `--translation-backend` | `gemini`／`argos` |
| `--gemini-model` | 初期モデル。現行既定は `gemini-3.1-flash-lite` |
| `--voice`、`--tts-volume`、`--tts-workers` | 音声・音量・音声生成並列数 |
| `--tts-timing-mode` | `strict`／`no-overlap` |
| `--subs-sync-mode` | `original`／`tts-aligned` |
| `--tts-backend` | `piper`（既定）／`azure`（公式Azure AI Speech） |
| `--embed-subs` | ソフト字幕出力。コンテナはmp4／mkv |
| `--embed-subs-format` | mkv字幕のsrt／ass指定 |
| `--burn-subs` | 焼き込み出力。`--embed-subs` と併用不可 |
| `--burn-subs-in-band`、`--burn-subs-band-ratio` | 焼き込み時の黒帯 |
| `--video-text-overlay` と関連引数 | 任意のOCR・映像テキスト置換。GUIでは無効 |
| `--force` | 出力の再生成。再開用の字幕も上書き対象になり得るため注意 |

OCRにはOpenCV・pytesseractのPython依存だけでなくTesseract本体・言語データ等の準備が必要です。同梱版からOCRは除外しています。XTTSと非公式Edge TTSは依存・CLI・自動フォールバックも含めて削除しました。GPU動作を標準インストーラーの保証範囲として案内しないでください。

### 音声合成の設定と配布条件

標準はPiper＋LJ Speechの米国英語女性音声です。Piper／eSpeak NGのGPLv3以降のライセンスと対応ソースを配布し、モデル作成者のパブリックドメイン表記・由来・ハッシュを保存します。大学教職員の講義英語化を想定しますが、非商用限定・研究限定モデルには依存しません。授業料や研究費の存在を理由に利用可能と断定するモデルは採用しません。詳細は[配布ガイド](PACKAGING.md)を参照してください。

男女選択はPiperの女性 `en_US-ljspeech-medium` と男性 `en_US-john-medium`、Azureの女性 `en-US-AriaNeural` と男性 `en-US-AndrewNeural` 等を使います。Johnは作成者のページで米国英語男性・パブリックドメインと明記されたモデルです。GUIのラベルをモデル名へ解決し、ジョブと音声キャッシュメタデータに保存します。登録済みのジョブは選択変更の影響を受けません。

Piperを直接importする同梱実行ファイルはGPLv3以降の組み合わせとして配布します。アプリの元のMIT表示は残しますが、同梱版全体をMITのみと案内しません。Piper・eSpeak NGに加え、実際に使ったアプリ／ビルドスクリプト／依存ロックの対応ソースも配布物に含めます。WindowsインストーラーもGPL本文と元のMIT表示を併記します。

Azureは `--tts-backend azure` を使い、`AZURE_SPEECH_KEY` と `AZURE_SPEECH_REGION` をworker環境変数へ渡します。キーはOS資格情報ストア、リージョンは非秘密の設定です。CLIでは利用者が環境変数を設定します。キーを引数やJSONへ書きません。Piper選択時はGUIが起動するworkerからAzureのキー・リージョンを除去します。キーとリージョンは実行時にも検査し、失敗時は別バックエンドへ切り替えません。Piperの男性音声をCLIで使う例は `--tts-backend piper --voice en_US-john-medium`、Azureの男性音声は `--tts-backend azure --voice en-US-AndrewNeural` です。既定の女性音声はそれぞれ `en_US-ljspeech-medium`／`en-US-AriaNeural` です。

旧版の待機ジョブに記録された音声・バックエンドは引き継げない場合があります。旧Edge／XTTSの指定を黙って変換せず、キャンセルして新しい方式で再登録してください。

### CLIの途中再開

CLIにはGUIのような `--japanese-srt`／`--english-srt` 引数はありません。`--outdir` に既存の `jp.srt` または `en.srt` を用意し、`--force` を付けずに実行すると工程の既存出力判定を利用できます。

英語SRTだけで文字起こしも省略する場合、GUIは空の `jp.srt` を同時に用意します。CLIで同じ動作を求める場合も、工程の依存関係を確認してください。別入力・別設定の中間ファイルを混在させると古い出力を再利用する可能性があります。

### GUIとCLIの出力差

CLIは `final.mp4`、字幕選択時の `final_soft_sub.mp4`／`final_sub.mp4` などを生成します。GUIの `元ファイル名_translated.mp4` への転送、DB更新、不要動画の削除は `LocalJobManager` の責務です。CLI単独では中間動画を自動削除しません。

## 5. 設定・環境変数

| 名前 | 用途・注意 |
|---|---|
| `GEMINI_API_KEY` | Gemini認証。キーをログ・Gitへ保存しない |
| `TRANSLATE_DATA_DIR` | マネージャーのデータ場所。Mac Finderランチャーは独自に設定 |
| `TRANSLATE_PYTHON` | Macインストーラー／シェルランナーのPython指定。対応バージョンを選ぶ |
| `TRANSLATE_WORKER_PYTHON` | GUIから起動する変換Python。Windowsランナーが設定 |
| `PYTHONPATH` | workerモジュールの解決。Windowsランナーが設定 |
| `GEMINI_FALLBACK_MODELS` | Geminiのモデル候補指定。処理詳細は `resolve_gemini_model_candidates()` を確認 |
| `GEMINI_DISCOVER_MODELS` | 利用可能モデル取得の有効・無効 |
| `GEMINI_BATCH_MAX_TEXTS` | 翻訳バッチの字幕行数上限 |
| `GEMINI_BATCH_MAX_CHARS` | 翻訳バッチの文字数上限 |
| `GEMINI_ONE_SHOT_MAX_TEXTS` | 一括送信からバッチ送信へ切り替える基準 |

Geminiのモデル可用性はAPI側に依存します。モデル名を変更した際は、フォールバック・モデル取得の動作も確認してください。Geminiの利用上限に達し候補モデルを使い切ると、Argosフォールバックを使います。翻訳バックエンドはGeminiとArgosです。

`settings.json` は出力先・音声とAPIキー画面の起動時表示設定を保持します。`hide_api_keys_on_startup` は文字列 `"true"`／`"false"` で保存し、未設定時は画面を表示します。表示はGUI構築後の `after_idle` で行い、「次回から表示しない」の変更時に保存します。翻訳バックエンド・文字起こしプリセットは起動時に既定へ戻ります。専門用語は既定CSVとユーザー追加CSVをマージし、重複を除きます。ジョブ用Whisperプロンプトは整形・長さ制限（現行480文字）の対象で、全用語を必ず反映するわけではありません。

GUIの「APIキー設定」はmacOS KeychainまたはWindows Credential Managerだけを使います。平文ファイルへの代替保存はありません。環境変数のキーがあればOS保存値より優先します。サービス名は `TranslateService`、保存名は `GEMINI_API_KEY`／`AZURE_SPEECH_KEY` です。キーはジョブJSON・引数・設定ファイルには保存せず、実行時に選択サービスのキーだけを子プロセス環境へ渡します。CLI単独では従来どおり環境変数を設定します。バックエンド障害の文面にキーが含まれる可能性があるため、その例外文面をGUIやログへ転記しないでください。

## 6. 成果物とクリーンアップ契約

### ジョブデータ

チェック対象の履歴削除は `delete_jobs()` を使用し、停止済みジョブのみ許可します。対象のログ・管理JSON・中間データ・DB行を削除しますが、全ジョブの完成動画／出力字幕を保護します。一覧はJob IDをTreeviewのitem IDとして更新し、チェック状態・行選択を毎秒の更新で失わないようにします。

GUIの一括中間削除は `delete_job_intermediates()` を使用します。登録済みの終端状態ジョブだけを対象とし、実行処理中または非終端状態があると拒否します。登録と削除は同じマネージャーロックで排他制御します。DB・管理JSON・ログ・登録済み全ジョブの完成動画／出力字幕は保持し、外部ディレクトリへのsymlinkは辿りません。別インスタンスとの排他制御はないため、利用者には他のインスタンスを終了するよう案内します。

| データ | 用途 |
|---|---|
| `input.mp4` | 登録時に作成する作業コピー |
| `audio.wav` | 文字起こし用抽出音声 |
| `jp.srt`／`jp_clean.srt` | 生の文字起こし／整形した日本語字幕 |
| `en.srt`／`en_aligned.srt` | 翻訳結果／音声配置へ同期した英語字幕 |
| `en.txt`／`en.mp3`／`en.mp3.meta.json` | 英語本文／音声／音声生成メタデータ |
| `whisper_prompt.txt` | ジョブの用語を反映したプロンプト |
| `video_text_regions.json`／`video_overlay.mp4` | 任意の映像テキスト置換結果 |
| `.asr_chunks/`／`.tts_segments/` | 分割処理用。通常処理で整理されても残る場合があります |
| `pipeline.log`／`status.json` | 工程と状態 |

GUIの出力字幕は、存在する `en_aligned.srt` と `jp.srt` をコピーします。生の日本語字幕を出す仕様を変更する場合はUI・再開動作・マニュアルを更新してください。

### 動画削除の不変条件

- 動画と存在する出力字幕のコピーがすべて成功した後にだけ動画整理を行う。
- ジョブディレクトリ内を再帰走査し、`VIDEO_SUFFIXES` の拡張子を大文字小文字を区別せず対象にする。
- `resolve()` で指定した出力パスを保護する。指定出力がジョブ内でも削除しない。
- 元入力、他ジョブ、出力先の無関係なファイルは操作しない。
- 変換・エクスポート失敗時は動画整理へ進まない。
- 動画削除の `OSError` を成功として隠さず、ジョブをエラーにする。
- 動画以外のファイル、過去の完了ジョブ、旧構成の生成物を一括削除しない。

動画削除に失敗すると、一部の動画は既に削除済みで、完成物は出力済みの場合があります。処理はトランザクション的な一括ロールバックではありません。成果物コピーも原子的なセット保存ではないため、途中失敗時は存在だけで正常出力と判断しないでください。

### 旧ツールの扱い

[bin/](../source/worker/bin/) のDB確認・移行・削除ツールは旧構成の固定パスやスキーマを前提とするものがあります。[cleanup_artifacts.sh](../source/worker/bin/cleanup_artifacts.sh) もworker直下を基準にするため、現在の `default_data_dir()` を一般的に解決するツールではありません。

現行GUIのジョブへ適用する前に、対象パス・SQL・削除一覧を実装で確認します。GUIの保存場所を確認せずに一斉削除・移行コマンドを実行しないでください。macOS標準Bashとツール内のBash機能の互換性にも注意します。

## 7. テストと手動確認

### 自動テスト

workerディレクトリから、そのアプリのPythonで実行します。

macOS：

```bash
cd source/worker
.venv/bin/python -m py_compile local_job_manager.py standalone_app.py tests/test_local_job_manager.py
.venv/bin/python -m unittest discover -s tests -v
```

Windows：

```powershell
cd source\worker
& .\.venv\Scripts\python.exe -m py_compile local_job_manager.py standalone_app.py tests/test_local_job_manager.py
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

現行テストはGUIのヘルパーをインポートするためTkinterが必要ですが、画面を開く操作はしません。ジョブ変換は疑似子プロセスで検証します。同梱workerの解決、外部Pythonの排除、APIキーの非永続化、保存後のキー削除も検証します。実API、実動画、字幕プレーヤー、OS別インストーラーの実行成功をこのテストだけで保証しません。実OSのキー保存・読み出し・削除は配布前に別途確認します。

カバーする主な項目：

- FIFO・完了・出力先と字幕保存
- 動画整理、指定出力保護、原本・他ジョブ・無関係な出力の保護
- 出力失敗時の動画保持、削除エラーの状態
- 失敗プロセスと入力検証
- SRTの登録、再開時の `--force` 抑制
- ログからの進捗、設定保存、用語CSV

### Windowsインストーラーの回帰テスト

リポジトリルートからWindows PowerShell 5.1で実行します。Pesterなどの追加依存は不要です。

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\win\tests\test_python_environment.ps1
```

PowerShell 7では `pwsh -NoProfile -File win/tests/test_python_environment.ps1` でも実行できます。検出結果と子プロセス応答はテスト内のフィクスチャを使い、一時ディレクトリ内で退避・作成を検証します。ユーザーのPython・レジストリ・ジョブ・環境を変更せず、実際のwinget導入も行いません。

主な対象は、PATHに3.8／3.14がある混在環境、Launcher／登録情報／標準導入先／PATH検出、32-bit・ARM64・不足モジュール・破損環境の拒否、正常環境の再利用、既存環境の退避、作成・pip失敗時の停止です。Windows実機でのLauncher・登録情報・winget動作確認は、別途スモークテストで行います。

### 手動スモークテスト

Mac・Windowsのそれぞれで、機密情報を含まない短い動画を使用します。

1. クリーンな取得フォルダーからインストーラーを実行する。
2. 正規のFinder／ショートカット経路でGUIを開く。
3. 動画のみで処理し、英語音声・埋め込み字幕・出力SRTを確認する。
4. 完了後、ジョブ内の不要動画がなく、原本と完成動画があることを確認する。
5. 日本語SRTのみ、英語SRTのみ、両SRTの登録を確認する。
6. 別出力先・音声・文字起こし設定を変え、設定保存範囲を確認する。
7. 複数ジョブを登録し、実行順と一覧順を確認する。
8. 待機中キャンセル・処理中キャンセル・終了時の動作を確認する。
9. 無効キーや書込み不可などの失敗がエラーになることを確認する。
10. 再起動後、保存場所・追加用語・履歴が想定どおりであることを確認する。

障害注入は専用のテストフォルダーだけで行います。共有・本番データの権限を変更して再現しないでください。

## 8. ビルド・更新・配布

初心者向けの同梱版は[同梱版ビルド・配布ガイド](PACKAGING.md)を参照してください。Gitにはコードと配布定義を保存し、生成物・モデル・ジョブ・キーを入れません。Windows SetupとmacOS DMGはOS別にビルドし、タグのワークフローは公開ではなくドラフトReleaseを作成します。署名・ライセンス・クリーンPCの確認後、保守担当者が公開します。以下の旧ビルドは開発用のソース実行経路です。

以下はリポジトリルートから実行します。

### macOSビルド

```bash
bash mac/build_app.sh
```

Command Line Toolsの `clang` とFoundationを使用し、実行したMacのアーキテクチャ向けにObjective-Cランチャーを生成します。出力は `mac/TranslateService.app`。Python環境はアプリ内へ同梱されず、プロジェクトの `.venv` を利用します。

### Windowsビルド

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\win\build_exe.ps1
```

既存の `.venv` が必要です。PyInstallerを確認・導入し、`--windowed --onedir` で `win\dist\TranslateService\TranslateService.exe` を生成します。変換workerは仮想環境のPythonです。

Pythonコードを変更した後、Windowsの生成済みGUIには再ビルドが必要です。プロンプト等のパッケージデータが凍結GUIの実行位置で利用できるかも確認してください。この旧ビルドを、全リソースを同梱した単体ポータブルアプリとして扱わないでください。

### 更新

Gitで取得した場合は、作業内容を確認・保管してから更新します。

```bash
git status --short
git pull --ff-only
```

未コミット変更がある場合や更新に失敗する場合は、上書き・強制リセットせず原因を確認します。依存変更・OS更新・環境変更に応じて標準インストーラーを再実行し、各OSでビルド・テストします。ZIP利用者向けの更新・データ移行は[ユーザーガイド](USER_GUIDE.md#9-更新バックアップアンインストール)を案内します。

### 配布前チェック

- [ ] macOS／Windowsそれぞれでクリーン導入とGUIスモークテストを行った
- [ ] APIキー、ジョブDB、ログ、入力・出力動画、個人の用語を含めていない
- [ ] `.venv` を別OS・別CPUへ配布していない
- [ ] 依存・実行資源・任意モデルのライセンスと利用条件を確認した
- [ ] 正規の起動経路とプロジェクトを保持する必要性を明記した
- [ ] [OS別導入](INSTALL_MAC.md)・[Windows導入](INSTALL_WINDOWS.md)・[ユーザーガイド](USER_GUIDE.md)・[調査手順](TROUBLESHOOTING.md)を更新した
- [ ] 同名出力の上書き、外部通信、終了・キャンセル時の制限を確認した

## 9. 保守上の注意と既知の制限

- GUIはPiper／Azure AI Speechを選択でき、MP4字幕埋め込みを使います。CLIの任意機能をGUIの機能と混同しないでください。
- 字幕埋め込みが失敗した場合、パイプラインは焼き込みへフォールバックします。FFmpegのlibass等が必要になる場合があります。
- 同名出力には上書き確認や世代管理がありません。出力名衝突のテストとユーザーへの警告を維持します。
- GUIにはエラー詳細を開く専用ボタン、履歴削除、既存ジョブ再試行、料金上限管理がありません。
- 文字起こしはfaster-whisper、翻訳は選択したバックエンド、音声はPiperまたは公式Azure。クラウド方式を選ぶとサービス通信があります。
- Azureには契約・キー・リージョン・料金の確認が必要です。アプリ内に課金上限はありません。
- 既定の工学用語CSVは通常起動で利用しますが、Windows凍結GUIのリソース解決はビルド検証対象です。
- `security.py` は一部の秘密表現をマスクしますが、全ログが機密を含まない保証はありません。ログ共有前に確認します。
- 現行のスクリプトは実行時に依存・外部サービスの可用性に左右されます。ドキュメント上の対応対象と、実際の検証結果を分けて記録します。

動作変更時は生産側だけでなく、GUI、CLI、マネージャー、OSランナー、テスト、マニュアルに同じ契約が伝わるように更新してください。
