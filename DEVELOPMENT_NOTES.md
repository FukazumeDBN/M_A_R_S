# M.A.R.S. 開発メモ

最終更新: 2026-09-03 JST

この文書は、次のセッションが短時間で現在地を把握するための技術引き継ぎメモである。製品要件と受け入れ条件は`DEVELOPMENT_REQUIREMENTS.md`、タスク状態と過去の細かな作業履歴は`/home/chappy/empire/data/empire.sqlite3`の`mars`プロジェクトを正本とする。この文書には現行実装と未確認事項だけを残し、廃止済み実装の試行履歴はDBノートを参照する。

## 現在地

| 項目 | 値 |
| --- | --- |
| アプリ | `/home/chappy/empire/projects/mars` |
| Forgeサーバー | `/home/chappy/empire/projects/minecraft-server/server` |
| ユーザー設定 | `~/.config/mars/settings.json` |
| 現在のtmuxセッション設定 | `Kichi_Craft_Pre` |
| 接続先 | `127.0.0.1:25565` |
| Minecraft / Forge / Java | 1.20.1 / 47.4.10 / OpenJDK 17 |
| GitHub | `https://github.com/FukazumeDBN/M_A_R_S`（公開） |
| ブランチ | `main` |
| 自動テスト | 31件（実Minecraftサーバーは操作しない） |
| systemdタイマー | 実環境では未有効化・未運用確認 |

Task 23〜35までの週間スケジュール、連動バックアップ、終了確認、再起動警告、横断監査、GitHub公開を`main`へ反映済み。リポジトリは公開設定で、直近の実装コミットは`12d79a2`。引き継ぎ時は、ユーザーの変更を失わないよう最初に`git status`と`git diff`を確認する。

## 再開時の確認

```bash
cd /home/chappy/empire
./bin/empire dashboard
./bin/empire task list --all --project mars

cd /home/chappy/empire/projects/mars
git status --short --branch
python3 -m compileall -q app tests
python3 -X dev -W error::ResourceWarning -m unittest discover -s tests -v
git diff --check
```

GUIはプロジェクト直下の`./run_mars.sh`で起動する。通常の自動テストは実サーバー、実tmux、systemdユーザー設定を変更しない。実環境確認は別途明示的に行う。

## コードの責務

| ファイル | 主な責務 |
| --- | --- |
| `app/mars/ui.py` | GTK画面、VTE接続、ユーザー操作、非同期処理 |
| `app/mars/server.py` | 状態取得、Start / Stop / Restart / Shutdown、停止待ち、プレイヤー追跡 |
| `app/mars/automation.py` | 再起動警告の時刻・表示方式・コマンドを一元管理 |
| `app/mars/terminal.py` | tmuxセッション、コマンド送信、子プロセス調査 |
| `app/mars/backup.py` | save制御、圧縮、ハッシュ、マニフェスト、世代保持 |
| `app/mars/scheduler.py` | systemdユーザーservice/timerの生成と反映 |
| `app/mars/settings.py` | XDG設定の読込、旧設定移行、型正規化、原子的保存 |
| `app/mars/jvm.py` | JVM引数の検証、バックアップ、原子的適用 |
| `app/mars/worker.py` | systemdから呼ばれる再起動・バックアップCLI |
| `app/mars/operations.py` | 再起動とバックアップのプロセス間排他 |

## 現行の重要な動作

- GUIと直接入力は同じtmuxセッションを操作し、VTEはtmuxへ直接attachする。GUI起動時にセッションを準備し、通常終了時はMinecraftの停止完了後にtmuxを終了する。
- ウィンドウを閉じる際、MinecraftがOnlineまたは起動途中なら確認する。正常停止を確認できなければ終了を中断してtmuxを残す。
- 手動Restartは、停止コマンド送信後にポート閉鎖、tmux配下の子プロセス終了、0.5秒の安定待機を確認してからStartを送る。
- 予定Restartは30分前にworkerを開始し、30分・10分・5分・3分・2分・1分・30秒・10秒前に警告する。10分前と1分前だけTitle、それ以外はsayを使う。
- 再起動連動バックアップは、停止完了→バックアップ→起動を共通ロック内で実行する。失敗時も可能なら起動復旧を試す。
- 稼働中バックアップは`save-off`→`save-all flush`→保存完了ログ確認→圧縮→`save-on`の順。起動・停止途中はバックアップしない。
- M.A.R.S.終了中は管理tmuxが存在しないため、予定workerはサーバーやtmuxを再作成せずスキップする。
- Log Counterは製品機能から削除済み。Overviewは大きなOnline/Offline表示、Port/Players/Mods、TPS/Pingで構成する。

## 2026-09-03 横断監査結果

- systemd oneshotの既定起動タイムアウトでは30分の警告待機が途中終了するため、生成serviceへ`TimeoutStartSec=2h`を追加した。10秒前警告の遅延を抑えるためtimerへ`AccuracySec=1s`も追加した。
- 警告待機中に30分間ロックを保持する処理を廃止した。M.A.R.S.終了を妨げず、予定時刻直前に管理tmuxの存在を再確認してから再起動ロックを取得する。待機時計はLinuxでサスペンド時間を含む`CLOCK_BOOTTIME`を優先する。
- 再起動警告の秒数、Title対象、GUI表示、systemdの先行起動時刻を`automation.py`の定義へ集約し、複数箇所の固定値がずれる余地を減らした。
- GUIの毎秒更新からtmux画面キャプチャを除去した。プレイヤー数は`latest.log`のinodeと読込位置を保持し、新しい追記だけを解析する。ログローテーションと途中行も処理する。
- Minecraftの起動・停止途中にバックアップを開始できる競合を拒否した。JVM追加引数のNUL文字も入力境界で拒否する。
- JVM設定値はGTKメインスレッドで取得してからworkerへ渡すよう変更し、バックグラウンドスレッドからGTK widgetへ触れる処理を解消した。
- 稼働中にサーバーディレクトリまたはtmuxセッション名を変更して、意図せずMinecraftを停止する操作を拒否した。
- Automation画面と要件書にあった「GUIを閉じても実行」という表記を、管理tmuxのライフサイクルに合わせて「M.A.R.S.起動中」に修正した。
- package初期化ファイルに残っていた意味のない重複文字列リテラルを削除した。
- restartとbackupのtimer適用または設定保存に失敗した場合、以前の設定から両timerを再生成するロールバックを追加した。GUI内に重複していたscheduler参照もApplicationServicesへ集約した。
- PyGObject import時の曖昧なGdkバージョン警告を解消し、GTK 3と同じGdk 3を明示した。

## 検証結果

- `python3 -m compileall -q app tests`: 成功。
- `python3 -X dev -W error::ResourceWarning -m unittest discover -s tests -v`: 31件成功。
- `shellcheck run_mars.sh`: 成功。
- `git diff --check`: 成功。
- 警告をエラー扱いしたGUI・worker import確認: 成功。
- 追加回帰テストは、systemd timeout/精度、警告待機中のロック解放とtmux消失時スキップ、起動途中バックアップ拒否、NUL拒否、追記・途中行・ログローテーション時のプレイヤー追跡、timer部分失敗時のロールバックを確認する。

## 未完了・実環境確認待ち

- systemdユーザータイマーを有効化した定期再起動、8回のゲーム内警告、連動・独立バックアップの実運用確認。
- 稼働中実Forgeサーバーでの`save-all flush`完了検出と手動バックアップ確認。
- 稼働中終了ダイアログのYES/NO双方、サーバー登録変更拒否、Automation画面の表示を実GUIで確認。
- TPS、Ping、PID、稼働時間、次回実行時刻、実行履歴はまだ実値を取得・表示していない。
- クラッシュ自動復旧、MOD / Config管理、バックアップ復元GUIは未実装。

## 記録ルール

1. 作業開始前に進捗DBをバックアップし、対象タスクを`in_progress`にする。
2. 安定した製品要件を変えたら`DEVELOPMENT_REQUIREMENTS.md`を更新する。
3. 現在値、検証結果、未コミット状態、次の一手だけをこの文書へ残す。細かな履歴はDBノートへ移す。
4. 作業終了時はDBへログを残し、タスクを実態に合う状態へ変更する。
5. Minecraftのワールド、個人設定、進捗DB、バックアップをGitHubへ含めない。
