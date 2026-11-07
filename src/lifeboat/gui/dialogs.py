"""Preferences, history and log dialogs."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from .. import system
from . import backend
from .setup_page import save_config, spin
from .widgets import STATUS, dialog_shell, icon_button, status_icon, text_button, when

NOTIFY = [("failure", "When something goes wrong"), ("always", "After every run"), ("never", "Never")]


class Preferences:
    def __init__(self, win):
        self.win = win
        self.cfg = backend.config_dict()
        s = self.cfg["settings"]
        self.dlg = Adw.PreferencesDialog()
        self.dlg.set_title("Preferences")
        page = Adw.PreferencesPage(title="General", icon_name="emblem-system-symbolic")

        g = Adw.PreferencesGroup(title="Notifications")
        self.notify_row = Adw.ComboRow(title="Notify me", model=Gtk.StringList.new([n[1] for n in NOTIFY]))
        keys = [n[0] for n in NOTIFY]
        self.notify_row.set_selected(keys.index(s["notify_on"]) if s["notify_on"] in keys else 0)
        g.add(self.notify_row)
        self.desktop_row = Adw.SwitchRow(title="Desktop notifications", active=s["notify_desktop"])
        g.add(self.desktop_row)
        self.ntfy_row = Adw.EntryRow(title="ntfy topic URL (optional), like https://ntfy.sh/my-backups")
        self.ntfy_row.set_text(s["ntfy_url"])
        g.add(self.ntfy_row)
        self.token_row = Adw.PasswordEntryRow(title="ntfy access token (optional; leave empty to keep)")
        g.add(self.token_row)
        page.add(g)

        g2 = Adw.PreferencesGroup(title="Storage")
        self.host_row = Adw.EntryRow(title=f"Folder name for this PC in destinations (default: {system.hostname()})")
        self.host_row.set_text(s["hostname"])
        g2.add(self.host_row)
        self.logs_row = spin("Logs kept per job", s["log_keep"], 1, 1000)
        g2.add(self.logs_row)
        page.add(g2)

        g3 = Adw.PreferencesGroup()
        save = text_button("Save", self._save, "suggested-action")
        save.add_css_class("pill")
        save.set_halign(Gtk.Align.CENTER)
        g3.add(save)
        page.add(g3)
        self.dlg.add(page)

    def present(self):
        self.dlg.present(self.win)

    def _save(self):
        fresh = backend.config_dict()
        s = fresh["settings"]
        s["notify_on"] = NOTIFY[self.notify_row.get_selected()][0]
        s["notify_desktop"] = self.desktop_row.get_active()
        s["ntfy_url"] = self.ntfy_row.get_text().strip()
        s["hostname"] = self.host_row.get_text().strip().lower()
        s["log_keep"] = int(self.logs_row.get_value())
        token = self.token_row.get_text().strip() or None

        def done(ok):
            if ok:
                self.dlg.close()
                self.win.toast("Preferences saved")
        save_config(self.win, fresh, done, ntfy_token=token)


class History:
    def __init__(self, win):
        self.win = win
        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        entries = backend.history(300)
        if not entries:
            g.add(Adw.ActionRow(title="Nothing has run yet"))
        for e in entries:
            label = STATUS.get(e.get("status"), ("", "", e.get("status", "")))[2]
            title = f"{e.get('job', '?')} · {e.get('action', 'backup')} · {label}"
            sub = f"{when(e.get('started'))} · {e.get('trigger', '')}"
            if e.get("message"):
                sub += f"\n{e['message']}"
            row = Adw.ActionRow(title=GLib.markup_escape_text(title), subtitle=GLib.markup_escape_text(sub))
            row.set_subtitle_lines(4)
            row.add_prefix(status_icon(e.get("status")))
            if e.get("log"):
                row.add_suffix(icon_button("document-properties-symbolic", "Show the log",
                                           lambda e=e: win.show_log(e["job"], e["log"])))
            g.add(row)
        page.add(g)
        self.dlg, _ = dialog_shell("History", page, width=680, height=640)

    def present(self):
        self.dlg.present(self.win)


class LogView:
    def __init__(self, win, job: str, name: str | None):
        self.win = win
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR,
                            left_margin=12, right_margin=12, top_margin=12, bottom_margin=12)
        view.get_buffer().set_text("Loading…")
        scroller = Gtk.ScrolledWindow(vexpand=True, child=view)
        self.dlg, _ = dialog_shell(f"Log: {job}" + (f" · {name[:-4]}" if name else ""), scroller, width=820, height=620)

        def done(text, err):
            view.get_buffer().set_text(err or text or "The log is empty.")
        win.helper.call("read_log", {"job": job, "name": name}, done)

    def present(self):
        self.dlg.present(self.win)
