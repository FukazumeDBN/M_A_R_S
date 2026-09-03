# M.A.R.S.

Minecraft Administration & Runtime Supervisorは、Ubuntu系LinuxでMinecraftサーバーを監視・操作・自動運用するGTKアプリケーション。

開発要件は[DEVELOPMENT_REQUIREMENTS.md](DEVELOPMENT_REQUIREMENTS.md)、現在地と引き継ぎ情報は[DEVELOPMENT_NOTES.md](DEVELOPMENT_NOTES.md)、進捗は`/home/chappy/empire/data/empire.sqlite3`の`mars`プロジェクトを正本とする。

## 起動

```bash
cd /home/chappy/empire/projects/mars
./run_mars.sh
```

起動すると登録済みサーバー用のtmuxセッションを自動準備し、右側のVTE端末を接続する。端末は直接入力でき、Start / Stop / Restartボタンも同じセッションを操作する。

右側の端末はマウスホイールでtmuxの履歴をスクロールできる。履歴を閲覧中は新しい出力で表示位置を強制的に末尾へ戻さず、最下部へ戻ると通常の自動追従に戻る。tmuxのマウス操作はM.A.R.S.が管理セッションへ自動適用する。

Automationタブでは、自動再起動と独立バックアップを月〜日のトグルボタン（黒=Enable、白=Disable）と共通時刻（24時間表記のHH:MM）で設定できる。予定再起動では30分・10分・5分・3分・2分・1分・30秒・10秒前にゲーム内警告を送り、10分前と1分前はTitle表示にする。バックアップを再起動連動にした場合は、再起動と同じ曜日・時刻に設定表示も同期し、停止完了確認→バックアップ→起動の順で処理する。

自動化はM.A.R.S.管理tmuxが存在する間だけ実行する。M.A.R.S.を閉じるとMinecraftとtmuxも終了するため、その間に到来した予定処理はサーバーやtmuxを勝手に再作成せずスキップする。

稼働中バックアップは`save-all flush`の完了を確認してから圧縮し、終了時には`save-on`の復旧を試みる。世代整理の対象はM.A.R.S.のマニフェストを持つアーカイブだけに限定する。

通常終了時はMinecraftへ正常停止コマンドを送り、停止を確認してから管理tmuxセッションを終了する。正常停止を確認できない場合は、安全のためアプリ終了を中断してセッションを残す。

Minecraftが稼働中または起動途中にウィンドウを閉じる場合は確認ダイアログを表示する。YESで安全停止後に終了し、NOではサーバーもM.A.R.S.も終了しない。

## テスト

通常のテストは実Minecraftサーバーを操作しない。

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## 現在の登録先

サーバーディレクトリはXDG設定`~/.config/mars/settings.json`へ保存する。現在のForgeサーバーは`/home/chappy/empire/projects/minecraft-server/server`にあり、M.A.R.S.本体とは物理的に分離されている。
