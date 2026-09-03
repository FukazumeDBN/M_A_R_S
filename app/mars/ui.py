from __future__ import annotations

import copy
import os
import signal
import threading
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gdk, GLib, Gtk, Pango, Vte

from .automation import restart_warning_summary
from .backup import BackupService
from .jvm import JvmArgumentFile
from .scheduler import ScheduleValidationError, WEEKDAYS, calendar_expression, validate_time
from .services import ApplicationServices
from .settings import AppSettings


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, application: Gtk.Application):
        super().__init__(application=application)
        self.set_title("M.A.R.S. – Minecraft Administration & Runtime Supervisor")
        self.set_default_size(1280, 760)
        self.set_size_request(980, 620)
        self.settings = AppSettings.load()
        self.services = ApplicationServices.build(self.settings, Path(__file__).resolve().parents[1])
        self.server = self.services.runtime
        self.busy = False
        self.refreshing = False
        self.closing = False
        self.shutdown_completed = False
        self.console_child_pid: int | None = None
        self.console_recovering = False
        self.console_auto_follow = True
        self._install_css()
        self._build_ui()
        self.connect("delete-event", self._on_delete_event)
        self.show_all()
        GLib.timeout_add_seconds(1, self._refresh_tick)
        self._refresh_tick()
        GLib.idle_add(self._initialize_terminal_session)

    def _install_css(self) -> None:
        provider = Gtk.CssProvider()
        provider.load_from_data(
            b"""
            window { background: #ffffff; color: #000000; font-family: Sans; }
            .brand { font-size: 14px; font-weight: bold; color: #000000; padding: 8px 0; }
            .sidebar { background: #eeeeee; border-right: 1px solid #000000; }
            .nav-button { min-height: 42px; min-width: 42px; margin: 4px 8px; padding: 0; border: 1px solid #000000; border-radius: 0; background: #dddddd; color: #000000; box-shadow: none; }
            .nav-button:hover { background: #ffffff; }
            .page { padding: 16px; background: #ffffff; }
            .page-title { font-size: 20px; font-weight: bold; color: #000000; }
            .muted { color: #555555; }
            .card { background: #ffffff; border: 1px solid #000000; border-radius: 0; padding: 16px; }
            .metric-row { min-height: 30px; }
            .metric-label { color: #000000; font-size: 13px; }
            .metric-field { background: #ffffff; border: 1px solid #000000; border-radius: 0; padding: 3px 6px; min-width: 72px; }
            .metric-value { color: #000000; font-size: 16px; font-weight: bold; }
            .status-value { font-size: 40px; font-weight: bold; }
            .status-online { color: #16823b; }
            .status-offline { color: #c62828; }
            .section-title { font-size: 15px; font-weight: bold; color: #000000; }
            .console-panel, .console-panel viewport, .console-panel vte-terminal { background-color: #000000; }
            .console-title { color: #eeeeee; background-color: #000000; border-bottom: 1px solid #555555; padding: 7px; }
            .console { background-color: #000000; color: #f0f0f0; }
            .statusbar { background: #ffffff; color: #000000; border-top: 1px solid #000000; padding: 5px 8px; }
            button, button.suggested-action, button.destructive-action { background: #dddddd; color: #000000; border: 1px solid #000000; border-radius: 0; box-shadow: none; background-image: none; }
            button:hover { background: #ffffff; }
            entry, spinbutton, combobox { background: #ffffff; color: #000000; border: 1px solid #000000; border-radius: 0; box-shadow: none; }
            .action-button { padding: 6px 12px; min-height: 30px; font-weight: bold; }
            .start-action { color: #16823b; }
            .stop-action { color: #c62828; }
            .restart-action { color: #24527a; }
            .schedule-day { min-width: 30px; min-height: 28px; padding: 2px 5px; background: #ffffff; color: #000000; }
            .schedule-day:checked { background: #000000; color: #ffffff; }
            .schedule-day:checked:hover { background: #222222; color: #ffffff; }
            """
        )
        screen = Gdk.Screen.get_default()
        if screen:
            Gtk.StyleContext.add_provider_for_screen(screen, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    @staticmethod
    def _label(text: str, css: str | None = None) -> Gtk.Label:
        label = Gtk.Label(label=text, xalign=0)
        if css:
            label.get_style_context().add_class(css)
        return label

    def _build_ui(self) -> None:
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        # The navigation rail is intentionally a fixed-width icon strip.  A
        # Gtk.Paned here makes it draggable and lets it consume page space.
        main = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        root.pack_start(main, True, True, 0)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sidebar.set_size_request(64, -1)
        sidebar.get_style_context().add_class("sidebar")
        sidebar.pack_start(self._label("M", "brand"), False, False, 4)
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        nav = [
            ("overview", "●", "Overview"),
            ("server", "▶", "Server"),
            ("mods-config", "◇", "Mods / Config (soon)"),
            ("automation", "◷", "Automation"),
            ("monitor", "◉", "Status monitor (soon)"),
        ]
        for name, icon, title in nav:
            button = Gtk.Button(label=icon)
            button.get_style_context().add_class("nav-button")
            button.set_size_request(44, 44)
            button.set_tooltip_text(title)
            button.connect("clicked", lambda _button, page=name: self.stack.set_visible_child_name(page))
            sidebar.pack_start(button, False, False, 0)
        main.pack_start(sidebar, False, False, 0)

        right = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        right.set_position(610)
        self.stack.set_vexpand(True)
        self._add_pages()
        right.add1(self.stack)

        console_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        console_box.get_style_context().add_class("console-panel")
        console_box.set_size_request(340, -1)
        self.console_title = self._label("Console — Offline", "console-title")
        console_box.pack_start(self.console_title, False, False, 0)
        scroll = Gtk.ScrolledWindow()
        scroll.get_style_context().add_class("console-scroll")
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.console = Vte.Terminal()
        self.console.set_input_enabled(True)
        self.console.set_scrollback_lines(10000)
        self.console.set_scroll_on_output(True)
        self.console.set_scroll_on_keystroke(True)
        self.console.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
        self.console.set_font(Pango.FontDescription("Monospace 10"))
        self.console.get_style_context().add_class("console")
        black = Gdk.RGBA()
        black.parse("#000000")
        white = Gdk.RGBA()
        white.parse("#f0f0f0")
        self.console.set_colors(white, black, [])
        self.console.connect("scroll-event", self._console_scroll_event)
        self.console.connect("child-exited", self._console_child_exited)
        scroll.add(self.console)
        console_box.pack_start(scroll, True, True, 0)
        right.add2(console_box)
        main.pack_start(right, True, True, 0)

        self.status_bar = Gtk.Label(label="Connecting…", xalign=0)
        # Operation results can include a full JVM argument string.  Keep the
        # status bar to one line so an unexpected long result never changes
        # the console's allocated height.
        self.status_bar.set_single_line_mode(True)
        self.status_bar.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_bar.get_style_context().add_class("statusbar")
        root.pack_end(self.status_bar, False, False, 0)
        self.add(root)

    def _console_scroll_event(self, _terminal: Vte.Terminal, event: Gdk.EventScroll) -> bool:
        """Let VTE scroll normally while controlling output auto-follow."""
        direction = event.direction
        scrolling_up = direction == Gdk.ScrollDirection.UP
        scrolling_down = direction == Gdk.ScrollDirection.DOWN
        if direction == Gdk.ScrollDirection.SMOOTH:
            has_deltas, _delta_x, delta_y = event.get_scroll_deltas()
            if has_deltas:
                scrolling_up = delta_y < 0
                scrolling_down = delta_y > 0

        if scrolling_up:
            self._set_console_auto_follow(False)
        elif scrolling_down:
            GLib.idle_add(self._resume_console_auto_follow_at_bottom)
        return False

    def _set_console_auto_follow(self, enabled: bool) -> None:
        self.console_auto_follow = enabled
        self.console.set_scroll_on_output(enabled)

    def _resume_console_auto_follow_at_bottom(self) -> bool:
        adjustment = self.console.get_vadjustment()
        bottom = adjustment.get_upper() - adjustment.get_page_size()
        if adjustment.get_value() >= bottom - 1:
            self._set_console_auto_follow(True)
        return False

    def _initialize_terminal_session(self) -> bool:
        if not self.server.configured:
            self.console_title.set_text("Console — Not configured")
            self._set_message("Server画面でMinecraftサーバーディレクトリを登録してください")
            return False
        self._run_async(
            "仮想ターミナルを準備中…",
            self.server.ensure_terminal,
            self._terminal_initialized,
        )
        return False

    def _terminal_initialized(self, result, error) -> None:
        if error:
            self._operation_finished("仮想ターミナルを準備しました", result, error)
            return
        try:
            self._attach_console()
        except Exception as exc:
            self._show_error(str(exc))
            self._set_message(f"ターミナル接続失敗: {exc}")
            return
        self._operation_finished("仮想ターミナルを準備しました", result, None)

    def _attach_console(self) -> None:
        if not self.server.configured:
            raise RuntimeError("Minecraftサーバーディレクトリが登録されていません")
        self._detach_console()
        success, child_pid = self.console.spawn_sync(
            Vte.PtyFlags.DEFAULT,
            str(self.server.server_dir),
            self.server.terminal_attach_argv(),
            None,
            GLib.SpawnFlags.DEFAULT,
            None,
            None,
            None,
        )
        if not success:
            raise RuntimeError("管理tmuxセッションへ接続できません")
        self.console_child_pid = child_pid
        self._set_console_auto_follow(True)
        self.console.grab_focus()

    def _detach_console(self) -> None:
        if self.console_child_pid is not None:
            try:
                os.kill(self.console_child_pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            self.console_child_pid = None
        self.console.reset(True, True)

    def _console_child_exited(self, _terminal: Vte.Terminal, _status: int) -> None:
        self.console_child_pid = None
        if not self.closing and self.server.configured and not self.console_recovering:
            GLib.timeout_add(500, self._recover_console)

    def _recover_console(self) -> bool:
        if self.closing or self.console_child_pid is not None or not self.server.configured:
            return False
        if self.busy:
            return True
        self.console_recovering = True
        self._run_async("仮想ターミナルへ再接続中…", self.server.ensure_terminal, self._console_recovered)
        return False

    def _console_recovered(self, result, error) -> None:
        self.console_recovering = False
        if error:
            self._operation_finished("仮想ターミナルへ再接続しました", result, error)
            return
        try:
            self._attach_console()
        except Exception as exc:
            self._operation_finished("仮想ターミナルへ再接続しました", result, exc)
            return
        self._operation_finished("仮想ターミナルへ再接続しました", result, None)

    def _add_pages(self) -> None:
        self.stack.add_named(self._overview_page(), "overview")
        self.stack.add_named(self._server_page(), "server")
        mods_config = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        mods_config.get_style_context().add_class("page")
        mods_config.pack_start(self._label("Mods / Config", "page-title"), False, False, 0)
        mods_config.pack_start(self._label("このタブはMOD・Config管理機能のために予約されています。", "muted"), False, False, 0)
        self.stack.add_named(mods_config, "mods-config")
        self.stack.add_named(self._automation_page(), "automation")
        for name, title in (("monitor", "Status monitor"),):
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            page.get_style_context().add_class("page")
            page.pack_start(self._label(title, "page-title"), False, False, 0)
            page.pack_start(self._label("この画面は次の開発フェーズで実装します。", "muted"), False, False, 0)
            self.stack.add_named(page, name)

    def _page_grid(self) -> Gtk.Grid:
        grid = Gtk.Grid(column_spacing=12, row_spacing=12)
        grid.get_style_context().add_class("page")
        grid.set_column_homogeneous(False)
        return grid

    @staticmethod
    def _metric_row(title: str, value: str) -> tuple[Gtk.Box, Gtk.Label]:
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=5)
        row.get_style_context().add_class("metric-row")
        title_label = Gtk.Label(label=f"{title}:", xalign=1.0)
        title_label.set_width_chars(8)
        title_label.get_style_context().add_class("metric-label")
        field = Gtk.Frame()
        field.get_style_context().add_class("metric-field")
        value_label = Gtk.Label(label=value, xalign=0.5)
        value_label.get_style_context().add_class("metric-value")
        field.add(value_label)
        row.pack_start(title_label, False, False, 0)
        row.pack_start(field, True, True, 0)
        return row, value_label

    def _overview_page(self) -> Gtk.Widget:
        grid = self._page_grid()
        grid.attach(self._label("Overview", "page-title"), 0, 0, 4, 1)
        grid.attach(self._label("ローカルMinecraftサーバーの状態", "muted"), 0, 1, 4, 1)

        display = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        display.set_hexpand(True)

        self.overview_status = Gtk.Label(label="確認中…", xalign=0.5)
        self.overview_status.get_style_context().add_class("status-value")
        status_block = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        status_block.set_size_request(150, -1)
        status_block.pack_start(self._label("Status", "muted"), False, False, 0)
        status_block.pack_start(self.overview_status, False, False, 0)
        display.pack_start(status_block, False, False, 0)

        display.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)

        metrics = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        metrics.set_size_request(145, -1)
        self.overview_port_row, self.overview_port = self._metric_row("Port", "25565")
        self.overview_players_row, self.overview_players = self._metric_row("Players", "-")
        self.overview_mods_row, self.overview_mods = self._metric_row("Mods", "0")
        for row in (self.overview_port_row, self.overview_players_row, self.overview_mods_row):
            metrics.pack_start(row, False, False, 0)
        display.pack_start(metrics, False, False, 0)

        display.pack_start(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL), False, False, 0)

        performance = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        performance.set_size_request(130, -1)
        performance.pack_start(self._label("Performance", "muted"), False, False, 0)
        self.overview_tps_row, self.overview_tps = self._metric_row("TPS", "-")
        self.overview_ping_row, self.overview_ping = self._metric_row("Ping", "-")
        performance.pack_start(self.overview_tps_row, False, False, 0)
        performance.pack_start(self.overview_ping_row, False, False, 0)
        display.pack_start(performance, False, False, 0)
        grid.attach(display, 0, 3, 4, 1)

        start = Gtk.Button(label="▶  Start")
        start.get_style_context().add_class("action-button")
        start.get_style_context().add_class("start-action")
        start.connect("clicked", self._server_action, "start")
        stop = Gtk.Button(label="■  Stop")
        stop.get_style_context().add_class("action-button")
        stop.get_style_context().add_class("stop-action")
        stop.connect("clicked", lambda _button: self._confirm_server_action("stop"))
        restart = Gtk.Button(label="↻  Restart")
        restart.get_style_context().add_class("action-button")
        restart.get_style_context().add_class("restart-action")
        restart.connect("clicked", lambda _button: self._confirm_server_action("restart"))
        buttons = Gtk.Box(spacing=8)
        for button in (start, stop, restart):
            buttons.pack_start(button, False, False, 0)
        grid.attach(buttons, 0, 5, 4, 1)
        return grid

    def _server_page(self) -> Gtk.Widget:
        grid = self._page_grid()
        grid.attach(self._label("Server", "page-title"), 0, 0, 3, 1)
        grid.attach(self._label("登録先と手動操作", "muted"), 0, 1, 3, 1)
        grid.attach(self._label("Server directory", "section-title"), 0, 3, 1, 1)
        self.server_entry = Gtk.Entry()
        self.server_entry.set_text(self.settings.server_dir)
        self.server_entry.set_hexpand(True)
        grid.attach(self.server_entry, 1, 3, 1, 1)
        choose_server = Gtk.Button(label="Browse…")
        choose_server.connect("clicked", self._choose_directory, self.server_entry, "Select server directory")
        grid.attach(choose_server, 2, 3, 1, 1)

        grid.attach(self._label("Session name", "section-title"), 0, 4, 1, 1)
        self.session_entry = Gtk.Entry()
        self.session_entry.set_text(self.settings.terminal.session_name)
        grid.attach(self.session_entry, 1, 4, 1, 1)

        save_path = Gtk.Button(label="Register / Prepare terminal")
        save_path.connect("clicked", self._save_server_path)
        grid.attach(save_path, 1, 6, 1, 1)
        grid.attach(self._label("Server control", "section-title"), 0, 8, 3, 1)
        controls = Gtk.Box(spacing=8)
        for action, title in (("start", "Start"), ("stop", "Stop"), ("restart", "Restart")):
            button = Gtk.Button(label=title)
            if action == "start":
                button.get_style_context().add_class("suggested-action")
            if action == "stop":
                button.get_style_context().add_class("destructive-action")
            callback = self._server_action if action == "start" else lambda _button, selected=action: self._confirm_server_action(selected)
            button.connect("clicked", callback, action) if action == "start" else button.connect("clicked", callback)
            controls.pack_start(button, False, False, 0)
        grid.attach(controls, 0, 9, 3, 1)

        grid.attach(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), 0, 11, 3, 1)
        grid.attach(self._label("JVM settings", "page-title"), 0, 12, 3, 1)
        grid.attach(self._label("Minimum memory", "section-title"), 0, 13, 1, 1)
        self.jvm_min_entry = Gtk.Entry()
        grid.attach(self.jvm_min_entry, 1, 13, 1, 1)
        grid.attach(self._label("Maximum memory", "section-title"), 0, 14, 1, 1)
        self.jvm_max_entry = Gtk.Entry()
        grid.attach(self.jvm_max_entry, 1, 14, 1, 1)
        grid.attach(self._label("Additional JVM arguments", "section-title"), 0, 15, 3, 1)
        jvm_scroll = Gtk.ScrolledWindow()
        jvm_scroll.set_size_request(-1, 100)
        self.jvm_custom_view = Gtk.TextView()
        self.jvm_custom_view.set_monospace(True)
        self.jvm_custom_buffer = self.jvm_custom_view.get_buffer()
        jvm_scroll.add(self.jvm_custom_view)
        grid.attach(jvm_scroll, 0, 16, 3, 1)
        apply_jvm = Gtk.Button(label="Apply JVM settings")
        apply_jvm.connect("clicked", self._apply_jvm_settings)
        grid.attach(apply_jvm, 1, 17, 1, 1)
        self._load_jvm_fields()

        page_scroll = Gtk.ScrolledWindow()
        page_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        page_scroll.add(grid)
        return page_scroll

    def _load_jvm_fields(self) -> None:
        if not self.settings.server_dir.strip():
            self.jvm_min_entry.set_text("1G")
            self.jvm_max_entry.set_text("2G")
            self.jvm_custom_buffer.set_text("")
            return
        config = JvmArgumentFile(Path(self.settings.server_dir)).load()
        self.jvm_min_entry.set_text(config.minimum_memory)
        self.jvm_max_entry.set_text(config.maximum_memory)
        self.jvm_custom_buffer.set_text(config.custom_arguments)

    def _apply_jvm_settings(self, _button: Gtk.Button) -> None:
        if not self.server.configured:
            self._show_error("先にMinecraftサーバーディレクトリを登録してください。")
            return
        if self.server.active():
            self._show_error("JVM設定はMinecraftサーバー停止中に適用してください。")
            return
        start, end = self.jvm_custom_buffer.get_bounds()
        custom = self.jvm_custom_buffer.get_text(start, end, False)
        minimum = self.jvm_min_entry.get_text()
        maximum = self.jvm_max_entry.get_text()
        manager = JvmArgumentFile(Path(self.settings.server_dir))
        self._run_async(
            "JVM設定を適用中…",
            lambda: manager.apply(minimum, maximum, custom),
            lambda _result, error: self._operation_finished(
                "JVM設定を適用しました", "user_jvm_args.txtへ反映済み", error
            ),
        )

    @staticmethod
    def _weekday_buttons(selected: list[str]) -> tuple[Gtk.Box, dict[str, Gtk.ToggleButton]]:
        labels = dict(zip(WEEKDAYS, ("月", "火", "水", "木", "金", "土", "日")))
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        buttons: dict[str, Gtk.ToggleButton] = {}
        for day in WEEKDAYS:
            button = Gtk.ToggleButton(label=labels[day])
            button.get_style_context().add_class("schedule-day")
            button.set_active(day in selected)
            button.set_tooltip_text(day)
            buttons[day] = button
            box.pack_start(button, False, False, 0)
        return box, buttons

    @staticmethod
    def _time_entry(value: str) -> Gtk.Entry:
        entry = Gtk.Entry()
        entry.set_width_chars(5)
        entry.set_max_length(5)
        entry.set_placeholder_text("HH:MM")
        entry.set_text(value)
        return entry

    def _automation_page(self) -> Gtk.Widget:
        grid = self._page_grid()
        grid.attach(self._label("Automation", "page-title"), 0, 0, 4, 1)
        grid.attach(self._label("M.A.R.S.起動中にsystemdユーザータイマーで実行されます", "muted"), 0, 1, 4, 1)

        grid.attach(self._label("Scheduled restart", "section-title"), 0, 3, 4, 1)
        grid.attach(self._label("Enabled", "muted"), 0, 4, 1, 1)
        self.restart_enabled = Gtk.Switch()
        self.restart_enabled.set_active(self.settings.restart.enabled)
        self.restart_enabled.set_halign(Gtk.Align.START)
        grid.attach(self.restart_enabled, 1, 4, 1, 1)
        grid.attach(self._label("Weekdays", "muted"), 0, 5, 1, 1)
        self.restart_days, self.restart_day_buttons = self._weekday_buttons(self.settings.restart.days)
        grid.attach(self.restart_days, 1, 5, 3, 1)
        grid.attach(self._label("Time", "muted"), 0, 6, 1, 1)
        self.restart_time = self._time_entry(self.settings.restart.time)
        grid.attach(self.restart_time, 1, 6, 1, 1)
        grid.attach(self._label(f"Warnings: {restart_warning_summary()}", "muted"), 0, 7, 4, 1)

        grid.attach(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL), 0, 8, 4, 1)
        grid.attach(self._label("Scheduled backup", "section-title"), 0, 9, 4, 1)
        grid.attach(self._label("Enabled", "muted"), 0, 10, 1, 1)
        self.backup_enabled = Gtk.Switch()
        self.backup_enabled.set_active(self.settings.backup.enabled)
        self.backup_enabled.set_halign(Gtk.Align.START)
        grid.attach(self.backup_enabled, 1, 10, 1, 1)
        grid.attach(self._label("Run with restart", "muted"), 0, 11, 1, 1)
        self.backup_linked = Gtk.Switch()
        self.backup_linked.set_active(self.settings.backup.linked_to_restart)
        self.backup_linked.set_halign(Gtk.Align.START)
        self.backup_linked.connect("notify::active", self._backup_link_changed)
        grid.attach(self.backup_linked, 1, 11, 1, 1)
        self.backup_schedule_label = self._label("Weekdays", "muted")
        grid.attach(self.backup_schedule_label, 0, 12, 1, 1)
        self.backup_days, self.backup_day_buttons = self._weekday_buttons(self.settings.backup.days)
        grid.attach(self.backup_days, 1, 12, 3, 1)
        grid.attach(self._label("Time", "muted"), 0, 13, 1, 1)
        self.backup_time = self._time_entry(self.settings.backup.time)
        grid.attach(self.backup_time, 1, 13, 1, 1)
        grid.attach(self._label("Destination", "muted"), 0, 14, 1, 1)
        self.backup_destination = Gtk.Entry()
        self.backup_destination.set_text(self.settings.backup.destination)
        self.backup_destination.set_hexpand(True)
        grid.attach(self.backup_destination, 1, 14, 1, 1)
        choose_backup = Gtk.Button(label="Browse…")
        choose_backup.connect("clicked", self._choose_directory, self.backup_destination, "Select backup destination")
        grid.attach(choose_backup, 2, 14, 1, 1)
        grid.attach(self._label("Keep archives", "muted"), 0, 15, 1, 1)
        self.keep_count = Gtk.SpinButton.new_with_range(1, 365, 1)
        self.keep_count.set_value(self.settings.backup.keep_count)
        grid.attach(self.keep_count, 1, 15, 1, 1)
        self.automation_summary = self._label("Not applied", "muted")
        grid.attach(self.automation_summary, 0, 16, 4, 1)
        backup_buttons = Gtk.Box(spacing=8)
        save_backup = Gtk.Button(label="Apply automation settings")
        save_backup.connect("clicked", self._save_automation_settings)
        manual_backup = Gtk.Button(label="Run backup now")
        manual_backup.connect("clicked", self._manual_backup)
        backup_buttons.pack_start(save_backup, False, False, 0)
        backup_buttons.pack_start(manual_backup, False, False, 0)
        grid.attach(backup_buttons, 0, 17, 4, 1)
        self.restart_time.connect("changed", self._restart_schedule_changed)
        for button in self.restart_day_buttons.values():
            button.connect("toggled", self._restart_schedule_changed)
        self._backup_link_changed(self.backup_linked, None)
        return grid

    def _backup_link_changed(self, switch: Gtk.Switch, _parameter) -> None:
        independent = not switch.get_active()
        if not independent:
            self._sync_linked_backup_schedule()
        self.backup_schedule_label.set_sensitive(independent)
        self.backup_days.set_sensitive(independent)
        self.backup_time.set_sensitive(independent)

    def _restart_schedule_changed(self, *_args) -> None:
        if hasattr(self, "backup_linked") and self.backup_linked.get_active():
            self._sync_linked_backup_schedule()

    def _sync_linked_backup_schedule(self) -> None:
        for day in WEEKDAYS:
            active = self.restart_day_buttons[day].get_active()
            self.backup_day_buttons[day].set_active(active)
        self.backup_time.set_text(self.restart_time.get_text())

    @staticmethod
    def _selected_weekdays(buttons: dict[str, Gtk.ToggleButton]) -> list[str]:
        return [day for day in WEEKDAYS if buttons[day].get_active()]

    def _choose_directory(self, _button: Gtk.Button, target: Gtk.Entry, title: str) -> None:
        # Let the desktop provide the folder picker instead of drawing an
        # application-specific dialog. On Linux Mint this uses the native GTK
        # file chooser (or the desktop portal when one is configured).
        dialog = Gtk.FileChooserNative.new(
            title,
            self,
            Gtk.FileChooserAction.SELECT_FOLDER,
            "_Select",
            "_Cancel",
        )
        current = Path(target.get_text()).expanduser() if target.get_text().strip() else Path.home()
        if current.is_dir():
            dialog.set_filename(str(current.resolve()))
        elif current.parent.is_dir():
            dialog.set_current_folder(str(current.parent.resolve()))
        response = dialog.run()
        if response == Gtk.ResponseType.ACCEPT:
            selected = dialog.get_filename()
            if selected:
                target.set_text(selected)
        dialog.destroy()

    def _save_server_path(self, _button: Gtk.Button) -> None:
        if not self.server_entry.get_text().strip():
            self._show_error("登録するサーバーディレクトリを選択してください。")
            return
        path = Path(self.server_entry.get_text()).expanduser().resolve()
        if not path.is_dir():
            self._show_error("登録するサーバーディレクトリが見つかりません。")
            return
        new_settings = copy.deepcopy(self.settings)
        new_settings.server_dir = str(path)
        new_settings.terminal.session_name = self.session_entry.get_text().strip()
        try:
            services = ApplicationServices.build(new_settings, Path(__file__).resolve().parents[1])
        except Exception as exc:
            self._show_error(str(exc))
            return
        old_server = self.server
        changed = old_server.server_dir != services.runtime.server_dir or old_server.terminal_attach_argv() != services.runtime.terminal_attach_argv()
        if changed and old_server.active():
            self._show_error("稼働中のMinecraftを停止してから、サーバーディレクトリまたはセッション名を変更してください。")
            return

        def register() -> str:
            if changed:
                old_server.shutdown()
            result = services.runtime.ensure_terminal()
            new_settings.save()
            return result

        def finished(result, error) -> None:
            if error:
                self._operation_finished("サーバーディレクトリを登録しました", result, error)
                return
            self.settings = new_settings
            self.services = services
            self.server = services.runtime
            self._load_jvm_fields()
            try:
                self._attach_console()
            except Exception as exc:
                self._operation_finished("サーバーディレクトリを登録しました", result, exc)
                return
            self._operation_finished("サーバーディレクトリを登録しました", result, None)

        self._run_async("仮想ターミナルを準備中…", register, finished)

    def _confirm_server_action(self, action: str) -> None:
        dialog = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.WARNING, buttons=Gtk.ButtonsType.OK_CANCEL, text=f"{action.capitalize()}を実行しますか？")
        dialog.format_secondary_text("稼働中のMinecraftサーバーへ実際に操作を送ります。")
        response = dialog.run()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self._server_action(None, action)

    def _server_action(self, _button: Gtk.Button | None, action: str) -> None:
        if action not in {"start", "stop", "restart"} or self.busy:
            return
        if not self.server.configured:
            self._show_error("先にMinecraftサーバーディレクトリを登録してください。")
            return
        method = getattr(self.server, action)

        def finished(result, error) -> None:
            self._operation_finished(f"{action}完了", result, error)

        self._run_async(f"{action}中…", method, finished)

    def _save_automation_settings(self, _button: Gtk.Button) -> None:
        restart_enabled = self.restart_enabled.get_active()
        backup_enabled = self.backup_enabled.get_active()
        linked = self.backup_linked.get_active()
        destination = self.backup_destination.get_text().strip()
        if not destination:
            self._show_error("バックアップ保存先を入力してください。")
            return
        if backup_enabled and linked and not restart_enabled:
            self._show_error("再起動連動バックアップを使う場合は、自動再起動も有効にしてください。")
            return
        try:
            restart_days = self._selected_weekdays(self.restart_day_buttons)
            if linked:
                self._sync_linked_backup_schedule()
            backup_days = restart_days if linked else self._selected_weekdays(self.backup_day_buttons)
            if restart_enabled and not restart_days:
                raise ScheduleValidationError("自動再起動は少なくとも1曜日を有効にしてください")
            if backup_enabled and not linked and not backup_days:
                raise ScheduleValidationError("独立バックアップは少なくとも1曜日を有効にしてください")
            restart_time = validate_time(self.restart_time.get_text())
            backup_time = restart_time if linked else validate_time(self.backup_time.get_text())
            fallback_restart_days = restart_days or self.settings.restart.days or [self.settings.restart.day]
            fallback_backup_days = backup_days or self.settings.backup.days or [self.settings.backup.day]
            if linked:
                fallback_backup_days = fallback_restart_days
            restart_expression = calendar_expression("weekly", ",".join(fallback_restart_days), restart_time)
            backup_expression = calendar_expression("weekly", ",".join(fallback_backup_days), backup_time)
        except ScheduleValidationError as exc:
            self._show_error(str(exc))
            return

        candidate = copy.deepcopy(self.settings)
        candidate.restart.enabled = restart_enabled
        candidate.restart.mode = "weekly"
        candidate.restart.days = fallback_restart_days
        candidate.restart.day = candidate.restart.days[0]
        candidate.restart.time = restart_time
        candidate.backup.enabled = backup_enabled
        candidate.backup.linked_to_restart = linked
        candidate.backup.mode = "weekly"
        candidate.backup.days = fallback_backup_days
        candidate.backup.day = candidate.backup.days[0]
        candidate.backup.time = restart_time if linked else backup_time
        candidate.backup.destination = destination
        candidate.backup.keep_count = self.keep_count.get_value_as_int()
        backup_summary = "restart連動" if backup_enabled and linked else backup_expression

        def finished(result, error) -> None:
            if error is None:
                self.settings = candidate
                self.automation_summary.set_text(f"Restart: {restart_expression} / Backup: {backup_summary}")
            self._operation_finished("自動化設定を適用しました", result, error)

        self._run_async(
            "自動化設定を適用中…",
            lambda: self._apply_automation(candidate),
            finished,
        )

    def _apply_automation(self, settings: AppSettings) -> str:
        return self.services.apply_automation(settings)

    def _manual_backup(self, _button: Gtk.Button) -> None:
        if self.busy:
            return
        if not self.server.configured:
            self._show_error("先にMinecraftサーバーディレクトリを登録してください。")
            return
        destination = Path(self.backup_destination.get_text()).expanduser()
        keep_count = self.keep_count.get_value_as_int()
        keep_days = self.settings.backup.keep_days
        self._run_async("バックアップ作成中…", lambda: BackupService(self.server, destination).create(keep_count, keep_days), lambda result, error: self._operation_finished("バックアップを作成しました", result, error))

    def _run_async(self, busy_message: str, worker, callback) -> None:
        if self.busy:
            return
        self.busy = True
        self._set_message(busy_message)

        def execute():
            try:
                result, error = worker(), None
            except Exception as exc:  # display the error in the UI thread
                result, error = None, exc
            GLib.idle_add(finish, result, error)

        def finish(result, error):
            self.busy = False
            callback(result, error)
            return False

        threading.Thread(target=execute, name="mars-worker", daemon=True).start()

    def _operation_finished(self, success_message: str, result, error) -> None:
        if error:
            self._show_error(str(error))
            self._set_message(f"失敗: {error}")
            return
        detail = str(result) if result is not None else "完了"
        self._set_message(f"{success_message}: {detail}")

    def _refresh_tick(self) -> bool:
        if self.shutdown_completed:
            return False
        if self.closing:
            return True
        if self.refreshing:
            return True
        self.refreshing = True

        def refresh():
            try:
                status = self.server.status()
                GLib.idle_add(self._apply_refresh, status)
            except Exception as exc:
                GLib.idle_add(self._apply_refresh_error, exc)

        threading.Thread(target=refresh, name="mars-refresh", daemon=True).start()
        return True

    def _apply_refresh(self, status) -> bool:
        self.refreshing = False
        if self.closing or self.shutdown_completed:
            return False

        state = "Online" if status.running and status.port_open else "Offline"
        terminal_state = "Online" if status.running else "Terminal ready" if status.terminal_running else "Offline"
        self.console_title.set_text(f"Console — {terminal_state}")
        detail = f"{state}  |  Forge 47.4.10  |  Port 25565  |  Players {status.players}  |  Mods {status.mods}  |  TPS {status.tps}  |  Ping {status.ping}"
        self.status_bar.set_text(detail)
        self.overview_status.set_text(state)
        status_context = self.overview_status.get_style_context()
        status_context.remove_class("status-online")
        status_context.remove_class("status-offline")
        status_context.add_class("status-online" if state == "Online" else "status-offline")
        self.overview_port.set_text(str(self.server.port))
        self.overview_players.set_text(status.players)
        self.overview_mods.set_text(status.mods)
        self.overview_tps.set_text(status.tps)
        self.overview_ping.set_text(status.ping)
        return False

    def _apply_refresh_error(self, error: Exception) -> bool:
        self.refreshing = False
        if self.closing or self.shutdown_completed:
            return False
        self.status_bar.set_text(f"状態取得失敗: {error}")
        return False

    def _set_message(self, message: str) -> None:
        # Avoid multiline/oversized worker results expanding the window layout.
        one_line = " ".join(str(message).split())
        maximum_length = 240
        if len(one_line) > maximum_length:
            one_line = f"{one_line[:maximum_length - 1]}…"
        self.status_bar.set_text(one_line)

    def _show_error(self, message: str) -> None:
        dialog = Gtk.MessageDialog(transient_for=self, flags=0, message_type=Gtk.MessageType.ERROR, buttons=Gtk.ButtonsType.OK, text="M.A.R.S.でエラーが発生しました")
        dialog.format_secondary_text(message)
        dialog.run()
        dialog.destroy()

    def _managed_server_is_active(self) -> bool:
        """Return whether closing the app would stop a running/starting server."""
        if not self.server.configured:
            return False
        return self.server.active()

    def _confirm_close_while_running(self) -> bool:
        dialog = Gtk.MessageDialog(
            transient_for=self,
            flags=0,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.NONE,
            text="サーバーが起動中です。本当に閉じますか？",
        )
        dialog.format_secondary_text("YESを選ぶとサーバーを停止してからM.A.R.S.を終了します。")
        dialog.add_buttons("YES", Gtk.ResponseType.YES, "NO", Gtk.ResponseType.NO)
        dialog.set_default_response(Gtk.ResponseType.NO)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.YES

    def _on_delete_event(self, _window, _event) -> bool:
        if self.shutdown_completed:
            return False
        if self.closing:
            return True
        if self._managed_server_is_active() and not self._confirm_close_while_running():
            return True
        self.closing = True
        self.busy = True
        self._set_message("Minecraftと仮想ターミナルを安全に終了中…")

        def shutdown() -> None:
            try:
                result, error = self.server.shutdown(), None
            except Exception as exc:
                result, error = None, exc
            GLib.idle_add(self._shutdown_finished, result, error)

        threading.Thread(target=shutdown, name="mars-shutdown", daemon=True).start()
        return True

    def _shutdown_finished(self, result, error) -> bool:
        self.busy = False
        if error:
            self.closing = False
            self._show_error(str(error))
            self._set_message(f"終了中断: {error}")
            return False
        self.shutdown_completed = True
        self._detach_console()
        self._set_message(str(result))
        self.destroy()
        return False

    def shutdown_managed_runtime(self) -> None:
        if self.shutdown_completed:
            return
        self.server.shutdown()
        self.shutdown_completed = True
