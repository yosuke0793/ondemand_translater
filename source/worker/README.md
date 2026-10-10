# 共通GUI・変換worker

このフォルダーにはTranslate ServiceのTkinter GUI、ローカルジョブ管理、動画変換パイプラインが入っています。

マニュアルはプロジェクトの [docs/](../../docs/) に集約しています。通常の利用と開発を分けて説明しています。

| 目的 | 文書 |
|---|---|
| Python不要のWindows／Mac同梱版 | [同梱版インストール手順](../../docs/INSTALL_BUNDLED.md) |
| Macへの導入 | [macOSインストール手順](../../docs/INSTALL_MAC.md) |
| Windowsへの導入 | [Windowsインストール手順](../../docs/INSTALL_WINDOWS.md) |
| GUIの操作・字幕・用語・保存場所 | [ユーザーガイド](../../docs/USER_GUIDE.md) |
| 起動・変換エラーの調査 | [トラブルシューティング](../../docs/TROUBLESHOOTING.md) |
| CLI・環境構築・テスト・ビルド | [開発者ガイド](../../docs/DEVELOPER_GUIDE.md) |

主要な入口は [standalone_app.py](standalone_app.py)、[local_job_manager.py](local_job_manager.py)、[lecture_poc/__main__.py](lecture_poc/__main__.py) です。依存関係は [requirements.txt](requirements.txt)、任意のXTTS関連依存は [requirements-xtts.txt](requirements-xtts.txt) を参照してください。

現行GUIはローカル子プロセスで変換します。同梱版は [bundled_worker.py](bundled_worker.py) を凍結したworker、ソース版は仮想環境のPythonを使います。[app_runtime.py](app_runtime.py) が資源と実行経路を分け、[credential_store.py](credential_store.py) がGUIで入力したAPIキーをOSの安全な保存先へ保管します。同梱版のビルドとGitHub Releasesは[配布ガイド](../../docs/PACKAGING.md)を参照してください。旧worker／Slurm構成のジョブ投入・DB移行手順は適用しません。[bin/](bin/) の既存ツールは使用前に対象パスとスキーマを確認してください。

GUIを閉じると停止要求を送ります。終了後のバックグラウンド継続は保証しません。また、CLI単独実行はGUIの成果物転送・不要動画削除を行わないため、[出力差](../../docs/DEVELOPER_GUIDE.md#guiとcliの出力差)を確認してください。
