"""Overview: the state of every job at a glance, with live progress."""

from __future__ import annotations

import time

from gi.repository import Adw, GLib, Gtk

from .. import APP_ID
from ..util import human_duration, human_size
from . import backend
from .widgets import DEST_ICONS, STATUS, icon_button, status_icon, text_button, when

STALE_DAYS = 7


class JobCard:
    """One PreferencesGroup per job; keeps references so progress can update in place."""

    def __init__(self, page: "OverviewPage", info: dict):
        self.page = page
        self.name = info["name"]
        g = self.group = Adw.PreferencesGroup(title=info["name"])
        if info.get("description"):
            g.set_description(info["description"])

        self.button = text_button("Back Up Now", self._on_button, "suggested-action")
        self.button.add_css_class("pill")
        self.button.set_margin_start(6)
        box = Gtk.Box(spacing=6)
        box.append(self.button)
        g.set_header_suffix(box)

        last = info.get("last_run")
        good = info.get("last_success")
        self.last_row = Adw.ActionRow()
        if last:
            if last["status"] in ("success", "warning"):
                self.last_row.set_title(f"Last backup {when(last.get('finished'))}")
            else:
                self.last_row.set_title(f"{STATUS.get(last['status'], ('', '', last['status']))[2]} {when(last.get('finished'))}")
            msg = last.get("message") or ""
            if last["status"] not in ("success", "warning") and good:
                msg += f" Last good backup {when(good.get('finished'))}."
            self.last_row.set_subtitle(GLib.markup_escape_text(msg))
            self.last_row.add_prefix(status_icon(last["status"]))
            if last.get("log"):
                self.last_row.add_suffix(icon_button("document-properties-symbolic", "Show the log",
                                                     lambda: page.win.show_log(self.name, last.get("log"))))
        else:
            self.last_row.set_title("Not backed up yet")
            self.last_row.set_subtitle("Run it now, or wait for its schedule.")
            self.last_row.add_prefix(Gtk.Image.new_from_icon_name("dialog-information-symbolic"))
        g.add(self.last_row)

        self.progress_row = Adw.ActionRow(title="Backing up")
        self.progress_row.add_prefix(Gtk.Image.new_from_icon_name("emblem-synchronizing-symbolic"))
        self.bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, hexpand=True)
        self.bar.set_size_request(160, -1)
        self.progress_row.add_suffix(self.bar)
        self.progress_row.set_visible(False)
        g.add(self.progress_row)

        sched = Adw.ActionRow(title=info["schedule"])
        extras = []
        if info.get("next_run"):
            extras.append(f"Next: {when(info['next_run'])}")
        if info.get("before_pacman"):
            extras.append("Also before pacman upgrades")
        if info.get("on_drive_connect"):
            extras.append("Also when the drive is plugged in")
        if extras:
            sched.set_subtitle(" · ".join(extras))
        sched.add_prefix(Gtk.Image.new_from_icon_name("x-office-calendar-symbolic"))
        g.add(sched)

        dest = Adw.ActionRow(title=f"Saves to {info['destination']}", subtitle=GLib.markup_escape_text(info["destination_label"]))
        dest.add_prefix(Gtk.Image.new_from_icon_name(DEST_ICONS.get(info.get("destination_type"), "folder-symbolic")))
        g.add(dest)

        what = Adw.ActionRow(title="Browse backups", subtitle=GLib.markup_escape_text(", ".join(info["module_names"]) or "Extra folders"))
        what.add_prefix(Gtk.Image.new_from_icon_name("document-open-recent-symbolic"))
        what.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
        what.set_activatable(True)
        what.connect("activated", lambda *_: page.win.show_backups(self.name))
        g.add(what)

        self.running = None
        self.set_running(info.get("running"))

    def set_running(self, st: dict | None) -> None:
        self.running = st
        self.progress_row.set_visible(st is not None)
        if st is None:
            self.button.set_label("Back Up Now")
            self.button.remove_css_class("destructive-action")
            self.button.add_css_class("suggested-action")
            return
        self.button.set_label("Cancel")
        self.button.remove_css_class("suggested-action")
        self.button.add_css_class("destructive-action")
        phase = st.get("phase") or "Working"
        pct = st.get("percent")
        self.progress_row.set_title(GLib.markup_escape_text(phase))
        if pct is not None and phase in ("Copying files", "Finishing"):
            self.bar.set_fraction(max(0.0, min(1.0, pct / 100)))
            bits = [f"{pct}%", human_size(st.get("bytes"))]
            if st.get("rate"):
                bits.append(st["rate"])
            if st.get("eta") and phase == "Copying files":
                bits.append(f"{st['eta']} left")
            self.progress_row.set_subtitle(" · ".join(bits))
        else:
            self.bar.pulse()
            self.progress_row.set_subtitle(f"Started {human_duration(time.time() - float(st.get('started') or time.time()))} ago")

    def _on_button(self) -> None:
        win = self.page.win
        if self.running:
            self.button.set_sensitive(False)
            win.helper.call("cancel", {"key": self.name},
                            lambda r, e: (self.button.set_sensitive(True), win.toast(e or "Stopping…")))
        else:
            self.button.set_sensitive(False)

            def done(r, e):
                self.button.set_sensitive(True)
                if e:
                    win.error("Couldn't start the backup", e)
                else:
                    win.toast(f"Backup '{self.name}' started")
                    GLib.timeout_add(700, self.page.tick)
            win.helper.call("start_backup", {"job": self.name}, done)


class OverviewPage(Gtk.Stack):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.cards: dict[str, JobCard] = {}
        self.extra_rows: list = []

        empty = Adw.StatusPage(icon_name=APP_ID, title="No backups set up yet",
                               description="Pick what to back up, where to keep it and when. "
                                           "Your first backup takes a few minutes to set up.")
        btn = text_button("Set Up a Backup", win.new_job, "suggested-action")
        btn.add_css_class("pill")
        existing = text_button("Use Existing Backups", win.use_existing, None)
        existing.add_css_class("pill")
        existing.set_tooltip_text("Reinstalled? Point Mikoshi at the drive or NAS that holds your backups.")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, halign=Gtk.Align.CENTER)
        box.append(btn)
        box.append(existing)
        empty.set_child(box)
        self.add_named(empty, "empty")

        self.prefs = Adw.PreferencesPage()
        self.add_named(self.prefs, "jobs")

        # Summary at the top
        self.summary_group = Adw.PreferencesGroup()
        sbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=6, margin_bottom=6)
        self.summary_icon = Gtk.Image(pixel_size=64)
        self.summary_title = Gtk.Label(css_classes=["title-2"], wrap=True, justify=Gtk.Justification.CENTER)
        self.summary_sub = Gtk.Label(css_classes=["dim-label"], wrap=True, justify=Gtk.Justification.CENTER)
        for w in (self.summary_icon, self.summary_title, self.summary_sub):
            sbox.append(w)
        self.summary_group.add(sbox)
        self.ops_group = Adw.PreferencesGroup(title="Running now")
        self.ops_group.set_visible(False)
        self.groups: list[Adw.PreferencesGroup] = []
        self.prefs.add(self.summary_group)
        self.prefs.add(self.ops_group)

        self.rebuild()
        GLib.timeout_add_seconds(1, self._tick_timer)
        GLib.timeout_add_seconds(15, self._rebuild_timer)

    def _tick_timer(self) -> bool:
        self.tick()
        return True

    def _rebuild_timer(self) -> bool:
        if self.win.is_visible():
            self.rebuild()
        return True

    def rebuild(self) -> None:
        jobs = backend.jobs_overview()
        self.set_visible_child_name("jobs" if jobs else "empty")
        for g in self.groups:
            self.prefs.remove(g)
        self.groups.clear()
        self.cards.clear()
        for info in jobs:
            card = JobCard(self, info)
            self.cards[info["name"]] = card
            self.groups.append(card.group)
            self.prefs.add(card.group)
        self._summary(jobs)
        self.tick()

    def _summary(self, jobs: list[dict]) -> None:
        now = time.time()
        failed = [j for j in jobs if j["last_run"] and j["last_run"]["status"] == "failed"]
        stale = [j for j in jobs if j["enabled"] and j["schedule"] != "Manual only"
                 and (not j["last_success"] or now - float(j["last_success"].get("finished") or 0) > STALE_DAYS * 86400)]
        never = [j for j in jobs if not j["last_success"]]
        if failed:
            icon, css = "dialog-error-symbolic", "error"
            title = "A backup needs attention" if len(failed) == 1 else f"{len(failed)} backups need attention"
            sub = "Last run of " + ", ".join(f"'{j['name']}'" for j in failed) + " failed."
        elif stale:
            icon, css = "dialog-warning-symbolic", "warning"
            title = "Backups are out of date"
            sub = ", ".join(f"'{j['name']}'" for j in stale) + f" hasn't succeeded in over {STALE_DAYS} days."
        elif never and len(never) == len(jobs):
            icon, css = "dialog-information-symbolic", "accent"
            title = "Ready for the first backup"
            sub = "Use Back Up Now, or wait for the schedule."
        else:
            icon, css = "emblem-ok-symbolic", "success"
            title = "Backups are up to date"
            newest = max((float(j["last_success"]["finished"]) for j in jobs if j["last_success"]), default=0)
            sub = f"Last backup {when(newest)}." if newest else ""
        self.summary_icon.set_from_icon_name(icon)
        for c in ("error", "warning", "accent", "success"):
            self.summary_icon.remove_css_class(c)
        self.summary_icon.add_css_class(css)
        self.summary_title.set_label(title)
        self.summary_sub.set_label(sub)
        self.summary_sub.set_visible(bool(sub))

    def tick(self) -> bool:
        running = backend.running()
        by_job = {s.get("job"): s for s in running if s.get("action") == "backup"}
        for name, card in self.cards.items():
            was = card.running is not None
            card.set_running(by_job.get(name))
            if was and name not in by_job:
                GLib.timeout_add(500, lambda: (self.rebuild(), False)[1])
        # Restores and pruning
        for r in self.extra_rows:
            self.ops_group.remove(r)
        self.extra_rows.clear()
        others = [s for s in running if s.get("action") != "backup"]
        for s in others:
            row = Adw.ActionRow(title=f"{s['action'].capitalize()} · {s.get('job')}",
                                subtitle=GLib.markup_escape_text(s.get("phase") or ""))
            row.add_prefix(Gtk.Image.new_from_icon_name("emblem-synchronizing-symbolic"))
            if s.get("percent") is not None:
                bar = Gtk.ProgressBar(valign=Gtk.Align.CENTER, fraction=min(1, s["percent"] / 100))
                bar.set_size_request(140, -1)
                row.add_suffix(bar)
            key = s.get("key")
            row.add_suffix(icon_button("process-stop-symbolic", "Stop",
                                       lambda k=key: self.win.helper.call("cancel", {"key": k}, lambda r, e: None)))
            self.ops_group.add(row)
            self.extra_rows.append(row)
        self.ops_group.set_visible(bool(others))
        return False
