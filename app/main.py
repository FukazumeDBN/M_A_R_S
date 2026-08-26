#!/usr/bin/env python3
import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk

from mars.ui import MainWindow


class MarsApplication(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="jp.local.mars", flags=0)
        self.window = None

    def do_activate(self):
        window = self.props.active_window
        if window is None:
            window = MainWindow(self)
            self.window = window
        window.present()

    def do_shutdown(self):
        if self.window is not None and not self.window.shutdown_completed:
            try:
                self.window.shutdown_managed_runtime()
            except Exception as exc:
                print(f"M.A.R.S. shutdown warning: {exc}")
        Gtk.Application.do_shutdown(self)


if __name__ == "__main__":
    raise SystemExit(MarsApplication().run(None))
