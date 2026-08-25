# ondemand_translater

日本語音声のMP4動画を読み込み、英語音声と英語字幕を付けたMP4動画を生成するローカルアプリケーションです。

macOSとWindowsで動作します。変換処理はローカルマシン上で実行され、ブラウザ、SSH、SCP、Slurmは使用しません。

## 構成

```text
ondemand_translater/
├── mac/                 # macOS用インストーラーとTranslateService.app
├── win/                 # Windows用インストーラー
├── source/worker/       # macOS/Windows共通の変換処理
│   ├── lecture_poc/
│   ├── standalone_app.py
│   ├── local_job_manager.py
│   └── requirements.txt
└── trashbin/            # 旧構成・不要ファイルの退避場所
```

## 共通仕様

- 入力：`.mp4`
- 翻訳：Gemini（デフォルト）
- 音声合成：Edge TTS
- 字幕：英語字幕をMP4へ埋め込み（デフォルト）
- 複数動画：登録順に1本ずつ処理
- ジョブ一覧：入力ファイル名、進捗率、現在の処理段階を表示
- 出力：指定ディレクトリへ`元ファイル名_translated.mp4`
- 字幕出力：英語字幕 `元ファイル名_translated.srt` と文字起こし日本語字幕 `元ファイル名_translated_ja.srt`
- 設定保存：出力先と音声選択を次回起動時に復元
- 専門用語：工学系のデフォルト用語集を起動時に読み込み、GUIからCSV入出力が可能
- 途中再開：日本語SRTまたは英語SRTを指定して、完了済みの工程をスキップ可能

## 事前準備

### Gemini APIキー

Geminiを使用する場合はAPIキーが必要です。APIキーはGitへコミットしたり、READMEやログへ記載したりしないでください。

### macOS

macOS版はApple Silicon（arm64）とIntel（x86_64）に対応しています。インストーラーを実行したMacのCPUに合わせて、ネイティブなアプリとPython環境を作成します。

必要なもの：

- macOS
- インターネット接続（初回インストール時）
- Homebrew
- Xcode Command Line Tools（アプリ再生成時のclang）

Homebrewがない場合は、先に公式サイトからインストールしてください。

```text
https://brew.sh/
```

### Windows

Windows版の対象はWindows 11 24H2 / 25H2、x64です。

必要なもの：

- Windows 11 x64
- インターネット接続（初回インストール時）
- `winget`
- Microsoft StoreのApp Installer

PowerShellで確認できます。

```powershell
git --version
winget --version
```

## macOSでのインストール

1. このリポジトリを取得します。

```bash
git clone git@github.com:yosuke0793/ondemand_translater.git
cd ondemand_translater
```

SSHキーを設定していない場合はHTTPSを使います。

```bash
git clone https://github.com/yosuke0793/ondemand_translater.git
cd ondemand_translater
```

2. Finderで次のファイルをダブルクリックします。

```text
mac/install.command
```

インストーラーは次を実行します。

- Apple SiliconまたはIntelの判定
- Homebrewの検出
- FFmpegの検出・導入
- Python 3.10〜3.13の検出・導入
- Tkinterの確認
- `source/worker/.venv`の作成
- `requirements.txt`のインストール
- `mac/TranslateService.app`の生成

専門用語の初期値は `source/worker/lecture_poc/prompts/engineering_terms.csv` から読み込まれます。
GUIの「一覧確認」「CSV読込」「CSV書出」から、現在の用語リストを確認・交換できます。
入力したテキストや読み込んだCSVの用語は既存リストへ蓄積され、ユーザー追加分はデータディレクトリの `engineering_terms_custom.csv` に保存されます。

HomebrewのPythonがTkinterを含まない場合は、Pythonのバージョンに対応する `python-tk` を自動導入します。Pythonが見つからない場合は `python@3.12`、次に `python@3.10` をフォールバックとして試します。

3. Gemini APIキーを設定します。Finderからアプリを起動する場合は、`launchctl`でユーザーセッションへ設定します。

```bash
launchctl setenv GEMINI_API_KEY "ここに新しいAPIキー"
```

設定後、Finderで次のアプリをダブルクリックします。

```text
mac/TranslateService.app
```

既に起動中の場合は、一度終了してから再起動してください。

ターミナルから起動する場合は、次のようにも設定できます。

```bash
export GEMINI_API_KEY="ここに新しいAPIキー"
cd source/worker
./run_standalone.sh
```

DeepLを使用する場合は、Geminiの代わりに次を設定し、GUIまたは実行設定でDeepLを選択します。

```bash
launchctl setenv DEEPL_API_KEY "ここに新しいAPIキー"
```

## Windowsでのインストール

1. Windows 11 x64へリポジトリを取得します。

```powershell
cd $HOME\Downloads
git clone https://github.com/yosuke0793/ondemand_translater.git
cd ondemand_translater
```

2. エクスプローラーで次のディレクトリを開きます。

```text
ondemand_translater\win
```

3. `install.cmd`をダブルクリックします。

PowerShellから実行する場合は次のとおりです。

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\win\install.ps1
```

インストーラーは次を実行します。

- Windows 11、x64の確認
- `winget`の確認
- Python 3.10〜3.13の検出・導入
- FFmpegの検出・導入
- `source\worker\.venv`の作成
- `requirements.txt`のインストール
- PyInstallerのインストール
- `win\dist\TranslateService\TranslateService.exe`の生成
- デスクトップショートカットの作成

Pythonが見つからない場合は、`winget` で Python 3.12 を導入し、利用できなければ Python 3.10 をフォールバックとして試します。

4. Gemini APIキーをユーザー環境変数へ設定します。

```powershell
[Environment]::SetEnvironmentVariable(
  "GEMINI_API_KEY",
  "ここに新しいAPIキー",
  "User"
)
```

設定後、PowerShellとデスクトップアプリを一度終了して、再度デスクトップの次のショートカットを起動してください。

```text
Translate Service
```

現在のPowerShellセッションだけで試す場合は次のとおりです。

```powershell
$env:GEMINI_API_KEY = "ここに新しいAPIキー"
.\win\run_standalone.ps1
```

DeepLを使用する場合は、ユーザー環境変数に`DEEPL_API_KEY`を設定し、GUIでDeepLを選択します。

```powershell
[Environment]::SetEnvironmentVariable(
  "DEEPL_API_KEY",
  "ここに新しいAPIキー",
  "User"
)
```

## 起動後の使い方

1. 「動画ファイル」で入力MP4を選択
2. 必要に応じて「日本語字幕 (.srt)」または「英語字幕 (.srt)」を選択
3. 「動画出力先」で出力先ディレクトリを選択
4. 必要に応じて専門用語を入力
5. 翻訳バックエンドを選択
6. 音声プルダウンから音声を選択
7. 「翻訳開始(キューに追加)」を押す
8. ジョブ一覧で進捗率と処理段階を確認
9. 完了後、「完了ファイルを開く」を押す

途中から再開する場合は、入力する字幕に応じて次のように指定します。

- 動画だけ：文字起こしから全処理を実行
- 動画 + 日本語SRT：文字起こしをスキップし、日本語字幕の整形以降を実行
- 動画 + 英語SRT：文字起こし・日本語字幕整形・翻訳をスキップし、英語音声生成以降を実行
- 動画 + 日本語SRT + 英語SRT：文字起こし・日本語字幕整形・翻訳をスキップし、英語音声生成以降を実行

字幕方式はGUIから変更できず、英語字幕のMP4埋め込みに固定されています。

GUI の通常ラベル、設定欄、ボタン、入力欄は約14ptの文字サイズで表示されます。ジョブ一覧は情報量を保つため、専用の小さめの文字サイズを使用します。

## 保存場所

### macOS

Finderから起動した場合：

```text
source/worker/data/
```

コマンドラインから起動した場合の既定値：

```text
~/Library/Application Support/TranslateService/
```

### Windows

```text
%LOCALAPPDATA%\TranslateService\
```

主な保存内容：

- `settings.json`：出力先と音声選択
- `jobs.db`：ジョブ一覧と状態
- `jobs/<job_id>/`：入力動画、中間ファイル、状態
- `logs/<job_id>.log`：変換ログ
- `engineering_terms_custom.csv`：GUIで追加・読み込んだ専門用語

完了時には、英語字幕の `元ファイル名_translated.srt` と文字起こし結果の日本語字幕 `元ファイル名_translated_ja.srt` も出力先へ保存されます。

## トラブルシュート

### Geminiで翻訳できない

APIキーが起動したアプリへ引き継がれているか確認してください。Finder起動のmacOSでは`launchctl setenv`、Windowsではユーザー環境変数を使用します。APIキーの設定後はアプリを再起動してください。

### FFmpegが見つからない

macOSではHomebrewのFFmpegを確認します。

```bash
/opt/homebrew/bin/ffmpeg -version
/usr/local/bin/ffmpeg -version
```

WindowsではPowerShellで確認します。

```powershell
ffmpeg.exe -version
```

### アプリが起動しない

macOS：

```text
source/worker/data/app-launch.log
```

Windows：

```text
win\run_standalone.ps1
```

を確認してください。

### WindowsでPowerShellがスクリプトを拒否する

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\win\install.ps1
```

### 依存関係を再インストールする

macOS：

```bash
cd ondemand_translater
mac/install.command
```

Windows：

```text
win/install.cmd
```

## 開発者向けテスト

macOSまたはLinuxで共通テストを実行できます。

```bash
cd source/worker
python3 -m py_compile local_job_manager.py standalone_app.py tests/test_local_job_manager.py
python3 -m unittest discover -s tests -v
```

macOSアプリを再生成する場合：

```bash
./mac/build_app.sh
```

Windows版のexeを再生成する場合は、Windows上で次を実行します。

```powershell
.\win\build_exe.ps1
```

## 注意事項

- 初回インストールとGemini翻訳にはインターネット接続が必要です。
- 長い動画では処理に時間がかかります。
- APIキーは共有・コミット・ログ出力しないでください。
- `mac/TranslateService.app`と`win/dist/`は各OS上で生成するため、Git clone直後に存在しなくても正常です。
- macOSとWindowsで`.venv`を共有しないでください。
