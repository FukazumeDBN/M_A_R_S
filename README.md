# M.A.R.S.

**Minecraft Administration & Runtime Supervisor**

M.A.R.S.は、Ubuntu系Linuxで動作するMinecraftサーバー管理GUIです。サーバーのあるディレクトリを登録すると、M.A.R.S.がtmuxセッションを準備します。Start / Stop / Restartボタンと埋め込みターミナルから、普段ターミナルで行うサーバー操作を実行できます。

## できること

- Minecraftサーバーディレクトリの登録
- tmuxを利用したサーバーの起動・停止・再起動
- サーバーコンソールのリアルタイム表示と直接入力
- マウスホイールによるコンソール履歴のスクロール
- Forgeサーバーのメモリ割り当て（`-Xms` / `-Xmx`）と追加JVM引数の設定
- 曜日・時刻を指定した自動再起動
- 自動再起動前のゲーム内警告
- 曜日・時刻を指定した自動バックアップ
- 再起動と連動した安全なバックアップ（停止完了 → バックアップ → 起動）

## 動作環境

- Ubuntu系Linux
- Python 3.10以降
- GTK 3、VTE 2.91
- tmux
- systemdユーザーセッション（Automationを使う場合）
- GUIログイン中のデスクトップ環境

## インストール

必要なパッケージをインストールします。

```bash
sudo apt update
sudo apt install git python3 python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91 tmux
```

リポジトリを取得します。

```bash
git clone https://github.com/FukazumeDBN/M_A_R_S.git
cd M_A_R_S
chmod +x run_mars.sh
```

M.A.R.S.はMinecraft本体、Forge、ワールドデータを同梱していません。Minecraftサーバーは別途用意してください。

## Minecraftサーバーの準備

M.A.R.S.は、登録したディレクトリ内の起動スクリプトを実行します。標準設定ではForgeサーバーの次の構成を想定しています。

```text
your-server/
├── run.sh
├── user_jvm_args.txt   # Forgeの標準構成では任意
└── world/
```

`run.sh`が存在し、実行できることを確認してください。

```bash
cd /path/to/your-server
chmod +x run.sh
```

初回起動時のEULAへの同意や、Forgeサーバー側の設定はMojang / Forgeの案内に従って、M.A.R.S.を登録する前に済ませてください。

## 起動と初期設定

```bash
cd M_A_R_S
./run_mars.sh
```

1. **Server**タブを開きます。
2. **Browse…**でMinecraftサーバーのディレクトリを選びます。
3. 必要ならtmuxの**Session name**を変更します。英数字、`.`、`-`、`_`を使用できます。
4. **Register / Prepare terminal**を押します。
5. **Overview**タブで**Start**を押してサーバーを起動します。

登録情報は`~/.config/mars/settings.json`に保存されます。M.A.R.S.を起動すると、登録済みサーバー用のtmuxセッションを自動的に準備します。

## コンソールの使い方

Overview右側の黒い画面は、単なるログ表示ではなく、管理対象tmuxセッションへ接続したターミナルです。

- 画面をクリックしてMinecraftコマンドを直接入力できます。
- 入力後にEnterを押すと、そのままサーバーへ送信されます。
- Start / Stop / Restartボタンも同じtmuxセッションを操作します。
- マウスホイールで過去の出力を確認できます。最下部へ戻ると新しい出力へ自動追従します。

M.A.R.S.を終了するときは、管理中のMinecraftサーバーとtmuxセッションも終了します。サーバー稼働中にウィンドウを閉じると確認ダイアログが表示され、**Yes**で停止して終了、**No**で操作をキャンセルします。停止を確認できない場合は、安全のためアプリを終了しません。

## JVM設定

ServerタブのJVM settingsでは、次を設定できます。

- Minimum memory（例: `1G`、`1024M`）
- Maximum memory（例: `4G`）
- Additional JVM arguments（例: `-XX:+UseG1GC`）

サーバーを停止した状態で値を入力し、**Apply JVM settings**を押してください。Forgeの`user_jvm_args.txt`へ反映されます。メモリ値は最小値が最大値以下である必要があり、追加引数欄へ`-Xms` / `-Xmx`を重複して書くことはできません。

## 自動再起動とバックアップ

Automationタブで設定します。

- **Enabled**で機能を有効化します。
- 月〜日のボタンを押して実行曜日を選びます。黒がEnable、白がDisableです。
- 時刻は24時間表記の`HH:MM`で指定します。
- **Apply automation settings**で保存・適用します。

自動再起動では、30分・10分・5分・3分・2分・1分・30秒・10秒前にゲーム内へ警告します。10分前と1分前はTitle、それ以外はチャットのSayコマンドで表示します。

自動バックアップを再起動と連動させる場合は、再起動と同じ曜日・時刻で実行されます。処理は次の順序です。

```text
サーバー停止 → 停止完了の確認 → ワールドのバックアップ → サーバー起動
```

連動させない場合は、バックアップ独自の曜日・時刻を設定できます。バックアップ先はワールドディレクトリの内側・外側には設定できません。バックアップ対象は現在`world`ディレクトリで、`session.lock`は除外されます。

自動化は、M.A.R.S.が管理対象tmuxセッションを保持している間だけ動作します。M.A.R.S.を閉じている間に、サーバーやtmuxセッションを勝手に作り直すことはありません。スケジュール機能を使う場合は、M.A.R.S.を起動したままにしてください。

## よくある問題

### `tmux`が見つからない

次を実行してtmuxをインストールしてください。

```bash
sudo apt install tmux
```

### サーバーを起動できない

登録したディレクトリに`run.sh`があるか、実行権限があるかを確認してください。

```bash
ls -l /path/to/your-server/run.sh
chmod +x /path/to/your-server/run.sh
```

また、サーバー側のEULA同意、Javaのバージョン、Forgeの起動要件も確認してください。

### 自動化が実行されない

次を確認してください。

- Automationタブで設定を有効化して適用したか
- 曜日と時刻が正しいか
- M.A.R.S.と管理対象tmuxセッションが起動しているか
- GUIログイン中のsystemdユーザーセッションが動作しているか

## 開発者向け情報

開発要件と設計方針は[DEVELOPMENT_REQUIREMENTS.md](DEVELOPMENT_REQUIREMENTS.md)、開発状況の引き継ぎ情報は[DEVELOPMENT_NOTES.md](DEVELOPMENT_NOTES.md)にまとめています。
