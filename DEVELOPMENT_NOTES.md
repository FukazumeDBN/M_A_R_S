# M.A.R.S. 開発メモ

最終更新: 2026-09-02 JST

この文書は、次のセッションがすぐ作業を再開するための技術メモである。製品要件と受け入れ条件の正本は`DEVELOPMENT_REQUIREMENTS.md`、タスク状態と作業履歴の正本は`/home/chappy/empire/data/empire.sqlite3`の`mars`プロジェクトとする。

## 現在地

| 項目 | 値 |
| --- | --- |
| アプリ | `/home/chappy/empire/projects/mars` |
| Forgeサーバー | `/home/chappy/empire/projects/minecraft-server/server` |
| ユーザー設定 | `~/.config/mars/settings.json` |
| 現在のtmuxセッション設定 | `Kichi_Craft_Pre` |
| 接続先 | `127.0.0.1:25565` |
| Minecraft / Forge / Java | 1.20.1 / 47.4.10 / OpenJDK 17 |
| GitHub | `https://github.com/FukazumeDBN/M_A_R_S`（非公開） |
| ブランチ | `main` |
| 自動テスト | 24件（実Minecraftサーバーは操作しない） |
| systemdタイマー | 実環境では未有効化・未運用確認 |

Task 23〜27に相当する週間スケジュール、連動バックアップ、終了確認、堅牢化は`main`の履歴へまとめる。引き継ぎ時は`git status`と直近の`git log`を確認し、残っている変更を消さないこと。

## 最初に確認するコマンド

```bash
cd /home/chappy/empire
./bin/empire dashboard
./bin/empire task list --all --project mars

cd /home/chappy/empire/projects/mars
git status --short --branch
python3 -X dev -W error::ResourceWarning -m unittest discover -s tests -v
```

GUIの起動はプロジェクト直下で`./run_mars.sh`を実行する。実サーバーの起動・停止を伴う確認は、通常の単体テストと分けて明示的に行う。

## コードの責務

| ファイル | 主な責務 |
| --- | --- |
| `app/mars/ui.py` | GTK画面、VTE接続、ユーザー操作、非同期処理の受け渡し |
| `app/mars/server.py` | Start / Stop / Restart / Shutdown、停止完了待ち、状態取得 |
| `app/mars/terminal.py` | tmuxセッションの作成、入力、画面取得、子プロセス調査 |
| `app/mars/backup.py` | save制御、ワールド圧縮、ハッシュ、マニフェスト、世代保持 |
| `app/mars/scheduler.py` | systemdユーザーservice/timerの生成と反映 |
| `app/mars/settings.py` | XDG設定の読込、旧設定の移行、型崩れの安全な正規化、原子的保存 |
| `app/mars/worker.py` | systemdから呼ばれる再起動・バックアップCLI |
| `app/mars/operations.py` | 再起動とバックアップに共通するプロセス間ロック |

通常終了は「必要なら確認 → Minecraftへstop → ポート閉鎖とtmux子プロセス終了を確認 → 0.5秒安定待ち → 管理tmux終了」の順で行う。再起動連動バックアップは「停止完了 → バックアップ → 起動」を1つの共通ロック内で行う。

## 2026-09-02 バックエンド監査

- 未登録状態でウィンドウを閉じた際に、存在しない稼働判定メソッドを呼ぶ可能性を修正した。
- 終了確認の起動途中判定を、任意のtmux子プロセスではなくMinecraftの`java`/`run.sh`プロセスに限定した。
- 停止待ちの各ポーリングで重複していたtmux存在確認を削減し、子プロセス木を1回だけ調べるようにした。
- 設定JSONが配列、未知キー、誤った型、無効なセッション名を含んでも、GUI起動を妨げないよう正規化した。
- 廃止済みの数値間隔フィールドと変換関数、未使用UIヘルパーを削除した。旧JSON内の数値間隔キーは安全に無視し、次回保存時に除去する。
- 稼働中バックアップは`save-all flush`送信後、`latest.log`に新しい保存完了メッセージが出るまで待ってから圧縮するようにした。
- バックアップ失敗時にも`save-on`を必ず試し、元の失敗と復旧失敗を両方報告するようにした。マニフェスト保存も一時ファイル経由の原子的更新へ変更した。
- バックアップは`save-on`復旧まで成功する前に完成名へ昇格しない。世代整理は正しいM.A.R.S.マニフェストを持つアーカイブだけを対象とし、名前が似ただけのファイルを削除しない。
- ワールド名によるサーバーディレクトリ外参照、tmuxコマンドのNUL文字、型が不正な保持設定を入力境界で拒否するようにした。
- Automation設定は検証・systemd反映が成功するまで現在設定へ確定せず、入力エラーや反映失敗でメモリ上の設定だけが変わる状態を防いだ。
- 終了中の状態更新がステータス表示を上書きしないよう、refreshコールバックを抑止した。
- サーバー停止状態でGUIを起動し、設定読込、tmux/VTE初期化、通常終了がエラーなく完了するスモーク確認を行った。Minecraft本体は起動していない。

## 未完了・実環境確認待ち

- systemdユーザータイマーを実際に有効化した定期再起動・定期バックアップの運用確認。
- 実Forgeサーバー上での新しい`save-all flush`完了検出と、稼働中手動バックアップの確認。
- 稼働中終了ダイアログのYES/NO双方をGUI上で手動確認。
- 再起動前の10分・5分・1分警告、失敗履歴、次回実行時刻表示。
- TPS/Ping/PID/稼働時間の実測、クラッシュ自動復旧、MOD / Config管理、バックアップ復元GUI。
- Automationの2つのsystemd unit反映を完全なトランザクションにする処理。現在も2つ目の反映だけ失敗した場合は部分反映になり得るため、エラー表示後に再適用して整合させる。

## 記録ルール

1. 作業開始前に進捗DBをバックアップし、対象タスクを`in_progress`にする。
2. 安定した製品要件を変えたら`DEVELOPMENT_REQUIREMENTS.md`を更新する。
3. 現在値、検証結果、未コミット状態、次の一手はこの文書へ更新する。
4. 作業終了時はDBへ短いログを残し、タスクを`done`または実態に合う状態へ変更する。
5. Minecraftのワールド、個人設定、進捗DB、バックアップをGitHubへ含めない。
