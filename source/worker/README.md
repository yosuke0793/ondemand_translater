# lecture_poc

講義動画の吹き替えと字幕翻訳を行う、macOS / Ubuntu 24.04 LTS 向けの PoC です。

## セットアップ

### Windows 11版インストーラー（x64）

Windows 11 24H2 / 25H2のx64環境では、プロジェクト内の `win/install.cmd` をダブルクリックしてインストールできます。内部でPowerShellの `win/install.ps1` を実行し、Python、FFmpeg、仮想環境、`requirements.txt`の依存関係を準備して、PyInstallerで `win/dist/TranslateService/TranslateService.exe` を生成し、デスクトップに `Translate Service.lnk` を作成します。

```text
ondemand_translater/win/install.cmd
```

事前にMicrosoft Storeの「App Installer」に含まれる `winget` が利用できる必要があります。インストール時には管理者権限の確認が表示される場合があります。初回インストールにはインターネット接続が必要です。デスクトップショートカットは `win/run_standalone.ps1` を経由してexeを起動し、変換本体は同じvenvのPythonで実行します。翻訳にGeminiを使う場合は、起動前に `GEMINI_API_KEY` をWindowsの環境変数へ設定してください。

```powershell
$env:GEMINI_API_KEY = "your_gemini_api_key"
```

対応対象はWindows 11 x64です。Windows 10、32-bit Windows、ARM版Windowsは初期版の対象外です。インストーラーを実行したWindows上でPython仮想環境を作成するため、macOSの `.venv` は共有しないでください。

### Finder用インストーラー（Apple Silicon / Intel Mac）

プロジェクト内の `mac/install.command` をFinderからダブルクリックすると、依存関係を準備して実行用アプリを生成します。対象はApple Silicon（arm64）とIntel（x86_64）のmacOSです。

```text
ondemand_translater/mac/install.command
```

インストーラーは、Homebrew、FFmpeg、Python 3.10〜3.13、Tkinter、Python仮想環境、`requirements.txt`の依存関係を確認・準備し、最後に `TranslateService.app` を更新します。初回実行にはインターネット接続が必要です。Homebrewが未導入の場合は、公式サイトの案内に従ってHomebrewを導入してから再実行してください。

インストール後は、`mac/TranslateService.app` をFinderでダブルクリックして起動します。APIキーはインストーラーへ保存されないため、起動前に `GEMINI_API_KEY` などを環境変数へ設定してください。

```bash
export GEMINI_API_KEY="your_gemini_api_key"
```

`mac/install.sh`は既存の `source/worker/.venv` を再利用し、依存関係を更新します。インストーラーを実行したMacのCPUアーキテクチャに合わせて、`mac/TranslateService.app`をネイティブに生成します。arm64用とIntel用の依存環境は共用せず、それぞれのMac上でインストールしてください。

ffmpeg（字幕焼き込み用に `subtitles` フィルタ対応ビルド）を入れます。

Ubuntu 24.04 LTS:

```bash
sudo apt update
sudo apt install -y ffmpeg curl
ffmpeg -hide_banner -filters | grep -E 'subtitles|ass'
```

macOS:

```bash
brew install ffmpeg-full
echo 'export PATH="$(brew --prefix ffmpeg-full)/bin:$PATH"' >> ~/.zshrc
source ~/.zshrc
ffmpeg -hide_banner -filters | grep -E 'subtitles|ass'
```

`grep subtitles` の出力に `subtitles` と `ass` が表示されれば準備完了です。

仮想環境を作成して有効化し、Python 依存関係をインストールします。

```bash
python3.10 -m venv .venv; .venv/bin/python -m pip install -r requirements.txt
```

Python は 3.10 以上 3.13 以下を使ってください。現状の依存関係では `argostranslate==1.9.6` が `sentencepiece==0.2.0` を経由して入り、Python 3.14 では wheel 解決の都合でソースビルドに落ちて失敗することがあります。

`python3` が 3.14 を指している環境では、上のように `python3.10` を明示してください。すでに 3.14 で `.venv` を作ってしまった場合は、いったん削除してから作り直します。

```bash
rm -rf .venv; python3.10 -m venv .venv; .venv/bin/python -m pip install -r requirements.txt
```

実行時は、依存関係を入れた同じ環境で `python -m lecture_poc ...` を使ってください。`python` が別の環境を向いている場合は、`.venv/bin/python -m lecture_poc ...` のように明示すると安全です。

日本語字幕の整形でローカル LLM を使う場合は、Ollama とモデルを入れます。

Ubuntu 24.04 LTS:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve
ollama pull qwen2.5:7b-instruct
```

`ollama serve` は常駐プロセスなので、別ターミナルで実行するか systemd などで常駐化してください。

macOS:

```bash
brew install ollama
brew services start ollama
ollama pull qwen2.5:7b-instruct
```

## 使い方

### スタンドアロンデスクトップ版（推奨）

この構成では、ブラウザ、FastAPI、SSH、SCP、Slurmを使わず、1台のmacOSまたはUbuntu上で処理します。Tkinterの画面から複数のmp4をキューへ追加でき、ジョブは登録順に1本ずつ実行されます。画面を閉じても、変換処理は子プロセスとして継続します。

macOSでは、Finderで `mac/TranslateService.app` をダブルクリックして起動できます。初回起動時は、コマンドライン起動と同じく `source/worker/.venv` の作成と不足パッケージのインストールを自動で行います。

Finderから起動した場合のジョブ・設定・ログは、macOSのアプリ権限制約を避けるため `source/worker/data` に保存されます。コマンドラインから起動した場合は従来どおり、macOSでは `~/Library/Application Support/TranslateService` が既定です。

```text
ondemand_translater/mac/TranslateService.app
```

別の場所へ移動して使う場合は、アプリバンドルと `source/worker` ディレクトリを同じプロジェクト構成のまま移動してください。macOSが初回起動時に確認を表示した場合は「開く」を選択します。

```bash
cd source/worker
./run_standalone.sh
```

`run_standalone.sh` は起動時にPython 3.10〜3.13を探し、`.venv` がなければ自動作成します。さらに、`requirements.txt` の不足パッケージを検出した場合だけ自動インストールします。初回起動時はインターネット接続が必要です。

UbuntuではTkinterを追加でインストールしてください。

```bash
sudo apt install -y python3-tk ffmpeg
```

macOSではPythonにTkが含まれているか確認できます。

```bash
python3 -m tkinter
```

データは既定でmacOSでは `~/Library/Application Support/TranslateService`、Linuxでは `~/.local/share/translate-service` に保存されます。保存先を変更する場合は `TRANSLATE_DATA_DIR` を指定します。デスクトップ画面の「動画出力先」から、完成した動画を保存するディレクトリをジョブごとに指定できます。既定の出力先はデータディレクトリ内の `outputs` です。

「動画出力先」と「音声」の選択は `settings.json` に自動保存され、次回起動時に復元されます。設定ファイルはデータディレクトリ内に作成されます。

```bash
TRANSLATE_DATA_DIR="$HOME/translate_service_data" ./run_standalone.sh
```

画面から指定した用語集はジョブ専用のWhisper初期プロンプトとして使用されます。GeminiまたはDeepLを使う場合は、起動前に `GEMINI_API_KEY` または `DEEPL_API_KEY` を環境変数へ設定してください。出力ファイルは元動画の拡張子を除いた名前に `_translated.mp4` を付けて、指定した出力先へ保存します。処理用の中間ファイルは各ジョブの `jobs/<job_id>` に残ります。

音声は画面のプルダウンから選択します。プルダウンには声質の概要とEdge TTSの音声名（例: `落ち着いた男性の声 ("en-US-AndrewNeural")`）を表示し、実行時には対応する音声名を変換処理へ渡します。

スタンドアロン版の主なファイルは次のとおりです。

- `standalone_app.py`: Tkinterデスクトップ画面
- `local_job_manager.py`: FIFOキュー、SQLite、子プロセス、状態遷移を管理
- `run_standalone.sh`: 仮想環境からデスクトップ版を起動
- `jobs/<job_id>/status.json`: 個別ジョブの状態
- `logs/<job_id>.log`: 変換コマンドの標準出力ログ

ジョブ一覧には進捗率と現在の処理段階も表示されます。進捗率は、文字起こしや英語音声生成など処理時間の長い段階を重くした固定重みで計算され、処理段階は `pipeline.log` の開始ログから更新されます。処理中は99%を上限とし、最終出力の検証が完了した時点で100%になります。

従来の `front/` と `worker.py` は、VPS・SSH・Slurmを利用する旧構成です。スタンドアロン版の通常起動では使用しません。

### 標準的な使用方法：

```bash
.venv/bin/python -m lecture_poc --input input.mp4 --outdir out --jp-clean-mode rule --transcription-preset balanced --voice en-US-AriaNeural --tts-volume +100% --translation-backend gemini --embed-subs
```

ソフト字幕を埋め込みたい場合は `--embed-subs` を使います（既定は MP4 コンテナ）。

```bash
.venv/bin/python -m lecture_poc --input input.mp4 --outdir out --jp-clean-mode rule --transcription-preset balanced --voice en-US-AriaNeural --tts-volume +100% --embed-subs
```

`--embed-subs-container mkv` を指定すると、mkv で出力できます。

```bash
.venv/bin/python -m lecture_poc --input input.mp4 --outdir out --embed-subs --embed-subs-container mkv --embed-subs-format srt
```

字幕を元動画に重ねず、動画下の黒帯に表示したい場合は `--burn-subs-in-band` を併用します。

```bash
.venv/bin/python -m lecture_poc --input input.mp4 --outdir out --jp-clean-mode rule --transcription-preset balanced --voice en-US-AriaNeural --tts-volume +100% --burn-subs --burn-subs-in-band
```

黒帯の高さは既定で字幕量に応じて自動調整され、4行程度の字幕も収まるように広めに確保します。手動でさらに広げたい場合は `--burn-subs-band-ratio` を指定します。

```bash
.venv/bin/python -m lecture_poc --input input.mp4 --outdir out --burn-subs --burn-subs-in-band --burn-subs-band-ratio 0.32
```

Whisper の `initial_prompt` は `lecture_poc/prompts/whisper_initial_prompt.txt` から自動読込されます（ファイルがある場合）。
別ファイルを使う場合は `--whisper-prompt-file` を指定してください。

翻訳バックエンドは既定で Gemini です。DeepL または Argos Translate を使う場合は `--translation-backend` で指定します。
英語音声合成は既定で EdgeTTS を使います。`--tts-backend xtts-v2` を指定すると XTTS v2 zero-shot を使いますが、その場合は `requirements-xtts.txt` の追加インストールが必要です。XTTS が失敗した場合は EdgeTTS にフォールバックします。

長尺動画は文字起こし時に自動分割されます（既定値: 600 秒チャンク、1.5 秒オーバーラップ）。
必要に応じて `--transcription-workers` でチャンクごとの文字起こしを並列化できます。
実行ログは出力ディレクトリの `pipeline.log` にも保存されます。
`generate_en_mp3` が遅い場合は `--tts-workers 2` などで edge-tts を並列化できます（503 が増える場合は 1 に戻してください）。
`--tts-workers 0` を指定すると、CPU コア数に応じて worker 数を自動設定します。
XTTS v2 を使う場合は参照音声を日本語元音声から自動抽出し、音声生成は安全のため逐次実行になります。
`--tts-timing-mode no-overlap`（既定）では、英語音声が長くなっても後続字幕の開始を自動で遅らせて音声オーバーラップを回避します。
`--subs-sync-mode tts-aligned`（既定）では、`--tts-timing-mode no-overlap` 時に動画表示用字幕を音声配置へ同期し、後半での字幕/音声ズレを抑えます。

### ローカル LLM 整形を利用する場合:

```bash
python -m lecture_poc --input input.mp4 --outdir out --transcription-preset high-accuracy --voice en-US-AndrewNeural --tts-volume +100% --jp-clean-mode ollama --ollama-model qwen2.5:7b-instruct --burn-subs
```

### DeepL 翻訳を利用する場合:

```bash
export DEEPL_API_KEY="your_deepl_api_key"
.venv/bin/python -m lecture_poc --input input.mp4 --outdir out --translation-backend deepl --jp-clean-mode rule --burn-subs
```

`--deepl-api-key` でも API キーを指定できます（未指定時は `DEEPL_API_KEY` を使用）。
DeepL は字幕行を内部でまとめてバッチ送信するため、1行ごとの API 呼び出しより通信回数を抑えます。

### Gemini 翻訳を利用する場合:

```bash
export GEMINI_API_KEY="your_gemini_api_key"
.venv/bin/python -m lecture_poc --input input.mp4 --outdir out --translation-backend gemini --gemini-model gemini-3.1-flash-lite --jp-clean-mode rule --burn-subs
```

`--gemini-api-key` でも API キーを指定できます（未指定時は `GEMINI_API_KEY` を使用）。
Gemini 翻訳でも SRT のインデックスとタイムスタンプは保持し、字幕テキスト行のみを英訳します。
Gemini 翻訳はまず全行を 1 回の API 呼び出しで送信し、失敗した場合のみ自動でバッチ分割にフォールバックします。
`--translation-backend gemini` の場合、Gemini のレート/クォータ上限に達したと判定したときは DeepL にフォールバックし、DeepL も上限到達時は Argos にフォールバックします。
`--translation-backend deepl` の場合は、DeepL 上限到達時に Argos へフォールバックします。
Gemini の呼び出し回数を減らしたい場合は、1 リクエストあたりのバッチ上限を環境変数で調整できます（既定値: `GEMINI_BATCH_MAX_TEXTS=120`, `GEMINI_BATCH_MAX_CHARS=48000`）。
Gemini 上限時にモデルを自動切替したい場合は、`GEMINI_FALLBACK_MODELS` に候補モデルをカンマ区切りで設定してください（左から順に試行）。
`GEMINI_FALLBACK_MODELS` を未設定のときは、既定で `gemini-2.5-pro,gemini-2.5-flash,gemini-1.5-pro,gemini-1.5-flash` の順に試行します（高性能モデル優先）。
実行時には Gemini Models API から利用可能モデルを取得し、使えるモデルだけで候補順を解決します（既定 ON）。

```bash
export GEMINI_BATCH_MAX_TEXTS=160
export GEMINI_BATCH_MAX_CHARS=64000
export GEMINI_FALLBACK_MODELS=gemini-2.5-flash,gemini-1.5-flash
export GEMINI_DISCOVER_MODELS=1
```

モデル自動検出を無効化したい場合:

```bash
export GEMINI_DISCOVER_MODELS=0
```

ffmpeg に `subtitles` フィルタ（libass 対応）が必要です。

Ollama に渡す字幕整形プロンプトは `lecture_poc/prompts/jp_clean_ollama.txt` で管理します。
入力文の差し込みには `{{INPUT_TEXT}}` プレースホルダを使います。
用語集は `lecture_poc/prompts/jp_terms_glossary.csv` で管理できます（`wrong,correct`）。
入力行に一致する用語だけが `{{GLOSSARY_HINTS}}` としてプロンプトに注入されます。
`--jp-clean-mode ollama` では字幕テキスト行をまとめてバッチ推論し、SRT のタイムスタンプは元のまま保持します。
既定では 1 ファイル分の字幕行を可能な限り一括で処理します。
Ollama 整形の出力は日本語を維持し、翻訳は行わない設定です。

## 文字起こしの品質向上

### 自動オーディオ前処理

パイプラインは動画から音声を抽出する際に、以下の前処理フィルタを自動適用します：

1. **音量正規化** (`loudnorm=I=-16`) — 音量を -16 LUFS に標準化
2. **ハイパスフィルタ** (`highpass=f=100:poles=2`) — 100Hz 以下の低周波ノイズ（ブーン、ハム）を除去
3. **ローパスフィルタ** (`lowpass=f=6500:poles=2`) — 高周波ノイズ（ヒス、サー）を抑制
4. **ダイナミック圧縮** (`acompressor=threshold=0.1:ratio=4:attack=0.01:release=0.1`) — 音量の急激な変化を平準化

これらの処理により、特に講義動画のような環境ノイズが多い音声からより正確な文字起こしが可能になります。

### 文字起こし精度の調整

**高精度が必要な場合：**
```bash
.venv/bin/python -m lecture_poc --input input.mp4 --outdir out --transcription-preset high-accuracy ...
```

**速度が重要な場合：**
```bash
.venv/bin/python -m lecture_poc --input input.mp4 --outdir out --transcription-preset balanced ...
```

**GPU(CUDA) が使える場合：**
```bash
.venv/bin/python -m lecture_poc --input input.mp4 --outdir out --transcription-preset balanced --transcription-device auto ...
```

**CPU を使って文字起こしを速くしたい場合：**
```bash
.venv/bin/python -m lecture_poc --input input.mp4 --outdir out --transcription-preset balanced --transcription-workers 4 --transcription-cpu-threads 4 --transcription-max-pending-chunks 8 ...
```

長尺音声では、チャンクの抽出順と SRT の出力順は保持したまま、チャンクごとの Whisper 推論だけを並列実行します。
`--transcription-workers 0` のときは CPU コア数に応じて worker 数を自動設定します。
`--transcription-cpu-threads 0` のときは、選ばれた worker 数で CPU コアを割った値を自動設定します。
`--transcription-max-pending-chunks` で、抽出済みチャンク数を制限してメモリと一時ファイルの増えすぎを抑えられます。
文字起こし後には、短時間で連続する同一字幕を自動で圧縮します。

オプション:

- `--force` は全出力を再計算します。
- `--transcription-preset` は文字起こし品質プリセットです。`high-accuracy` / `balanced` / `fast` から選べます（既定値: `balanced`）。
- `--model` は faster-whisper のモデルサイズを手動指定します。未指定時はプリセット既定値を使います。
- `--whisper-prompt-file` は Whisper の `initial_prompt` を読み込むテキストファイルです。未指定時は `lecture_poc/prompts/whisper_initial_prompt.txt` を参照します。
- `--voice` は edge-tts の音声を指定します。既定値は `en-US-AriaNeural` です。
- `--tts-workers` は edge-tts の並列実行数です。`0` で自動設定、既定値は `1`（安定優先）です。速度重視なら `2` から試してください。
- `--tts-timing-mode` は音声配置モードです。`no-overlap`（既定）は後続字幕を自動で遅らせて重なりを回避し、`strict` は元のタイムスタンプどおりに配置します。
- `--tts-backend` は英語音声合成バックエンドです。`edge-tts`（既定）または `xtts-v2` を選べます。XTTS を選ぶと、日本語元音声から自動抽出した参照音声を使って XTTS を試し、失敗時は EdgeTTS にフォールバックします。
- `--subs-sync-mode` は動画表示に使う英語字幕の時刻モードです。`tts-aligned`（既定）は no-overlap 時に字幕時刻も音声配置へ同期し、`original` は翻訳直後の元時刻を使います。
- `--tts-volume` は edge-tts の音量です。既定値は `+100%`（約2倍）です。`+200%` で約3倍にできます。
- `--chunk-seconds` は文字起こし自動分割のチャンク長（秒）です。既定値は `600` です。`0` 以下で無効化できます。
- `--chunk-overlap-seconds` はチャンク境界で重ねる秒数です。既定値は `1.5` です。
- `--transcription-workers` は長尺音声のチャンク文字起こしを並列実行する worker 数です。既定値は `0` で、CPU コア数から自動設定します。
- `--transcription-cpu-threads` は faster-whisper が使う CPU スレッド数です。既定値は `0` で、選ばれた worker 数に応じて自動設定します。
- `--transcription-max-pending-chunks` は抽出済みで文字起こし待ちのチャンク上限です。既定値は `0` で、worker 数に応じて自動設定します。
- `--transcription-device` は faster-whisper の実行デバイスです。`cpu` / `cuda` / `auto` から選べます（既定値: `auto`）。
- `--transcription-compute-type` は faster-whisper の `compute_type` を手動指定します（例: `int8`, `int8_float16`, `float16`, `float32`）。未指定時はプリセット既定値を使います。
- `--lowpass-cutoff-hz` は高周波ノイズ除去用 lowpass のカットオフ周波数（Hz）です。既定値は `6500` です。`0` 以下で無効化できます。
- `--jp-clean-mode` は日本語字幕の整形方法です。`off` / `rule` / `ollama` から選べます（既定値: `rule`）。
- `--ollama-model` は `--jp-clean-mode ollama` で使うローカルモデル名です（既定値: `qwen2.5:7b-instruct`）。
- `--translation-backend` は翻訳バックエンドです。`argos` / `deepl` / `gemini` から選べます（既定値: `gemini`）。
- `--deepl-api-key` は DeepL API キーです。未指定時は環境変数 `DEEPL_API_KEY` を使用します。
- `--gemini-api-key` は Gemini API キーです。未指定時は環境変数 `GEMINI_API_KEY` を使用します。
- `--gemini-model` は `--translation-backend gemini` で使うモデル名です。既定値は `gemini-3.1-flash-lite` です。
- `--burn-subs` は英語字幕を焼き込んだ `final_sub.mp4` を作成します。
- `--embed-subs` は英語字幕を埋め込んだ `final_soft_sub.*` を作成します（`--burn-subs` と同時指定不可）。
- `--embed-subs-container` は `--embed-subs` 時の出力コンテナです。`mp4` / `mkv` から選べます（既定値: `mp4`）。
- `--embed-subs-format` は `--embed-subs` 時の字幕フォーマットです。`srt` / `ass` から選べます（既定値: `srt`）。
- XTTS v2 を使うには `python -m pip install -r requirements-xtts.txt` を追加で実行してください。

### 自動オーディオ前処理
文字起こしプリセットの内容:

- `high-accuracy`: `large-v3`, beam=10, best_of=5, CPU `float32`（高精度・低速）
- `balanced`: `medium`, beam=5, best_of=3, CPU `int8`（既定・バランス）
- `fast`: `small`, beam=1, best_of=1, CPU `int8`（高速）

### 音声オプション一覧

#### 男性

- en-US-AndrewNeural, 低めで落ち着く。説明系に合う
- en-US-BrianNeural, カジュアルで聞き取りやすい
- en-US-ChristopherNeural, しっかりしたトーンで情報系向き
- en-US-GuyNeural, 張りがあり、強調したい内容向き
- en-GB-RyanNeural, 英国アクセントでクリア

#### 女性

- en-US-AriaNeural, 明瞭で安定。ニュース読み風で講義向け
- en-US-EmmaNeural, 明るく自然。長尺でも聞き疲れしにくい
- en-US-JennyNeural, やわらかめで親しみやすい
- en-US-AvaNeural, 会話調で滑らか
- en-GB-SoniaNeural, 英国アクセントで落ち着いた印象


## 出力

このコマンドは出力ディレクトリに次のファイルを作成します。

- `audio.wav`
- `jp.srt`
- `jp_clean.srt`（フィラーや言い淀みを整形した日本語字幕）
- `en.srt`
- `en.txt`
- `en.mp3`（`en.srt` のタイムコードに合わせて配置した英語音声）
- `final.mp4`
- `--burn-subs` を使った場合は `final_sub.mp4`
- `--embed-subs` を使った場合は `final_soft_sub.mp4`（`--embed-subs-container mkv` の場合は `final_soft_sub.mkv`）
- `--burn-subs-in-band` を使うと、`final_sub.mp4` は動画下に黒帯を追加した字幕表示になります。
- `--burn-subs-band-ratio` は黒帯の高さ比率です。`0` 以下では字幕量に応じて自動調整し、4行程度の字幕を想定した帯サイズを使います。
- `--embed-subs` で字幕埋め込みが失敗した場合は、自動で `final_sub.mp4` の字幕焼き込みにフォールバックします。

## 安全な一斉削除

ログや生成物をまとめて消す場合は、専用スクリプトを使ってください。
デフォルトは dry-run で、実際には削除されません。

```bash
chmod +x bin/cleanup_artifacts.sh
./bin/cleanup_artifacts.sh
./bin/cleanup_artifacts.sh --apply
```

キュー状態ファイル（`queue/*.json`, `processing/*.json`, `done/*.json`, `error/*.json`）まで消す場合だけ、明示的に `--include-queue` を付けます。

```bash
./bin/cleanup_artifacts.sh --apply --include-queue
```

`jobs/*/input.mp4` は安全のため削除対象に含めていません。

## ジョブキュー（シーケンシャル処理）

`worker.py` は SQLite データベースを使用してジョブを管理し、**1 つずつ順に処理**します。これにより Gemini API などの制限に対応し、安定した運用が可能になります。

### キュー管理の流れ

1. `queue/` フォルダに `*.json` ファイルを配置
2. `worker.py` が起動時に `queue/` をスキャンし、DB に登録（未登録のジョブのみ）
3. DB から最も古い `queued` ジョブを取得
4. 実行中のジョブが 0 個なら、新規ジョブを Slurm に投入
5. ジョブ完了時に `status.json` を更新し、DB からは削除
6. 完了したジョブファイルを `done/` or `error/` に移動

### 初回セットアップ（既存キューがある場合）

```bash
# 既存の queue/ ジョブを DB に登録
python3 bin/migrate_queue_to_db.py
```

### ジョブ状態の確認

```bash
# すべてのジョブを表示
python3 bin/list_jobs_db.py

# 特定状態のジョブのみ表示（queued|running|done|error）
python3 bin/list_jobs_db.py queued
python3 bin/list_jobs_db.py running
python3 bin/list_jobs_db.py error
```

### テスト用ジョブの作成と削除

```bash
# 3 個のテストジョブを queue/ に作成
python3 bin/create_test_jobs.py

# worker.py を起動
python3 -m worker

# 別の端末でジョブ状態を監視
python3 bin/list_jobs_db.py
```

テスト用ジョブを削除する場合：

```bash
# DRY-RUN（プレビュー）
python3 bin/cleanup_test_jobs.py

# 実際に削除（DB とファイルシステム）
python3 bin/cleanup_test_jobs.py --apply
```

`cleanup_test_jobs.py` は `test-sequential-job-*` パターンのジョブを DB および `queue/`, `processing/`, `done/`, `error/` から削除します。

完了済み・失敗済みジョブを DB から手動で消したい場合は、以下を使います。

```bash
python3 bin/purge_finished_jobs_db.py
```

## 制限事項

- Argos Translate の翻訳品質は、有償のクラウド翻訳サービスより控えめです。
- edge-tts は非公式インターフェースを使うため、将来の挙動変更の影響を受ける可能性があります。
- `--burn-subs` は ffmpeg のビルドに `subtitles` フィルタ（libass）が含まれていないと失敗します。
- 動画の長さや解像度により処理時間が長くなるため、CPU 実行時は `--transcription-workers` や `--transcription-cpu-threads` の調整を推奨します。