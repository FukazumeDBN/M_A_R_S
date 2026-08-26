# M.A.R.S.

Minecraft Administration & Runtime Supervisorは、Ubuntu系LinuxでMinecraftサーバーを監視・操作・自動運用するGTKアプリケーション。

開発要件は[DEVELOPMENT_REQUIREMENTS.md](DEVELOPMENT_REQUIREMENTS.md)、進捗は`/home/chappy/empire/data/empire.sqlite3`の`mars`プロジェクトを正本とする。

## 起動

```bash
cd /home/chappy/empire/projects/mars
./run_mars.sh
```

起動すると登録済みサーバー用のtmuxセッションを自動準備し、右側のVTE端末を接続する。端末は直接入力でき、Start / Stop / Restartボタンも同じセッションを操作する。

右側の端末はマウスホイールでtmuxの履歴をスクロールできる。履歴を閲覧中は新しい出力で表示位置を強制的に末尾へ戻さず、最下部へ戻ると通常の自動追従に戻る。tmuxのマウス操作はM.A.R.S.が管理セッションへ自動適用する。

通常終了時はMinecraftへ正常停止コマンドを送り、停止を確認してから管理tmuxセッションを終了する。正常停止を確認できない場合は、安全のためアプリ終了を中断してセッションを残す。

## テスト

通常のテストは実Minecraftサーバーを操作しない。

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## 現在の登録先

サーバーディレクトリはXDG設定`~/.config/mars/settings.json`へ保存する。現在のForgeサーバーは`/home/chappy/empire/projects/minecraft-server/server`にあり、M.A.R.S.本体とは物理的に分離されている。
