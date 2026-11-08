"""Application entry point and main window."""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from .. import APP_ID, APP_NAME, __version__  # noqa: E402
from . import theme  # noqa: E402
from .backend import HelperClient  # noqa: E402
from .backups_page import BackupsPage  # noqa: E402
from .dialogs import History, LogView, Preferences  # noqa: E402
from .overview_page import OverviewPage  # noqa: E402
from .setup_page import DestinationsPage, JobEditor, JobsPage  # noqa: E402


class Window(Adw.ApplicationWindow):
    def __init__(self, app: "App"):
        super().__init__(application=app, title=APP_NAME)
        self.set_default_size(860, 760)
        self.set_size_request(380, 400)
        self.helper = app.helper
        self.helper.on_state_change = self._on_helper_state

        self.toasts = Adw.ToastOverlay()
        self.stack = Adw.ViewStack()
        self.overview = OverviewPage(self)
        self.backups = BackupsPage(self)
        self.jobs = JobsPage(self)
        self.destinations = DestinationsPage(self)
        self.stack.add_titled_with_icon(self.overview, "overview", "Overview", "emblem-ok-symbolic")
        self.stack.add_titled_with_icon(self.backups, "backups", "Backups", "document-open-recent-symbolic")
        self.stack.add_titled_with_icon(self.jobs, "jobs", "Jobs", "emblem-system-symbolic")
        self.stack.add_titled_with_icon(self.destinations, "destinations", "Destinations", "drive-harddisk-symbolic")
        self.stack.connect("notify::visible-child-name", self._on_page)

        header = Adw.HeaderBar()
        switcher = Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE)
        header.set_title_widget(switcher)
        menu = Gio.Menu()
        menu.append("History", "win.history")
        menu.append("Preferences", "app.preferences")
        menu.append(f"About {APP_NAME}", "app.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       tooltip_text="Main menu", primary=True))
        self.lock_icon = Gtk.Image.new_from_icon_name("changes-allow-symbolic")
        self.lock_icon.set_tooltip_text("Administrator access granted for this session")
        self.lock_icon.set_visible(False)
        header.pack_start(self.lock_icon)

        bottom = Adw.ViewSwitcherBar(stack=self.stack)
        self.banner = Adw.Banner()
        view = Adw.ToolbarView()
        view.add_top_bar(header)
        view.add_top_bar(self.banner)
        view.set_content(self.stack)
        view.add_bottom_bar(bottom)
        self.toasts.set_child(view)
        self.set_content(self.toasts)

        # Narrow windows (phones, tiled halves) move the page switcher to the bottom.
        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 560sp"))
        bp.add_setter(bottom, "reveal", True)
        bp.add_setter(switcher, "visible", False)
        self.add_breakpoint(bp)

        self.check_config()
        act = Gio.SimpleAction.new("history", None)
        act.connect("activate", lambda *_: History(self).present())
        self.add_action(act)

    # -- navigation helpers used by pages --------------------------------------
    def _on_page(self, *_):
        if self.stack.get_visible_child_name() == "backups":
            self.backups.refresh_jobs()

    def show_backups(self, job: str) -> None:
        self.stack.set_visible_child_name("backups")
        self.backups.select_job(job)

    def new_job(self) -> None:
        JobEditor(self, None).present()

    def use_existing(self) -> None:
        """First run on a reinstalled PC: add the destination, then offer what's stored there."""
        from .setup_page import DestEditor
        DestEditor(self, None, find_after=True).present()

    def show_log(self, job: str, name: str | None) -> None:
        LogView(self, job, name).present()

    def check_config(self) -> None:
        from . import backend
        backend.load_config()
        if backend.CONFIG_ERROR:
            self.banner.set_title("The config file has an error. Fix it with sudo mikoshi config edit.")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)

    def reload_all(self) -> None:
        self.check_config()
        self.overview.rebuild()
        self.jobs.refresh()
        self.destinations.refresh()
        self.backups.refresh_jobs()

    def toast(self, text: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=4))

    def error(self, heading: str, body: str) -> None:
        d = Adw.AlertDialog(heading=heading, body=body)
        d.add_response("ok", "OK")
        d.present(self)

    def _on_helper_state(self, unlocked: bool) -> None:
        self.lock_icon.set_visible(unlocked)


class App(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.helper = HelperClient()
        self.win: Window | None = None
        for name, cb in (("preferences", self._prefs), ("about", self._about), ("quit", lambda *_: self.quit())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        self.set_accels_for_action("app.quit", ["<Control>q"])
        self.set_accels_for_action("window.close", ["<Control>w"])

    def do_activate(self):
        if self.win is None:
            theme.apply()
            self.win = Window(self)
        self.win.present()

    def do_shutdown(self):
        self.helper.stop()
        Adw.Application.do_shutdown(self)

    def _prefs(self, *_):
        Preferences(self.win).present()

    def _about(self, *_):
        about = Adw.AboutDialog(
            application_name=APP_NAME, application_icon=APP_ID, version=__version__,
            developer_name="NullWRX", license_type=Gtk.License.GPL_3_0,
            website="https://github.com/NullWRX/Mikoshi",
            issue_url="https://github.com/NullWRX/Mikoshi/issues",
            comments="Snapshot backups for Arch Linux, built on rsync.")
        about.present(self.win)


def main() -> int:
    return App().run(sys.argv)
