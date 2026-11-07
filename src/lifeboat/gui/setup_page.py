"""Jobs page: create and edit backup jobs and destinations."""

from __future__ import annotations

import copy

from gi.repository import Adw, GLib, Gtk

from .. import config, destinations, schedule, system
from ..modules import registry
from ..util import LifeboatError, human_size
from . import backend
from .widgets import DEST_ICONS, alert, clear_group, confirm, dialog_shell, icon_button, text_button

MODES = [("manual", "Manual only"), ("hourly", "Every hour"), ("daily", "Every day"), ("weekly", "Every week"),
         ("monthly", "Every month"), ("custom", "Custom (systemd calendar)")]
DAYS = [("mon", "Monday"), ("tue", "Tuesday"), ("wed", "Wednesday"), ("thu", "Thursday"), ("fri", "Friday"),
        ("sat", "Saturday"), ("sun", "Sunday")]
DEST_TYPES = [("folder", "Folder", "A folder on a drive that's always mounted"),
              ("drive", "Drive", "A drive Lifeboat mounts when needed, like a USB drive"),
              ("nfs", "Network share (NFS)", "A share on a NAS")]


def default_job_dict() -> dict:
    return config.to_dict(config.Config(jobs={"x": config.Job()}))["jobs"]["x"]


def save_config(win, cfg_dict: dict, on_done, ntfy_token=None) -> None:
    """Validate locally (fast feedback), then save through the helper."""
    backend.load_config()
    if backend.CONFIG_ERROR:
        alert(win, "The config file has an error",
              "Lifeboat won't overwrite it. Fix it with `sudo lifeboat config edit`, then try again.\n\n"
              + backend.CONFIG_ERROR)
        on_done(False)
        return
    cfg, errors = config.from_dict(cfg_dict)
    errors = errors or config.validate(cfg)
    if errors:
        alert(win, "Check these settings", "\n".join(f"• {e}" for e in errors))
        on_done(False)
        return
    params = {"config_dict": cfg_dict}
    if ntfy_token is not None:
        params["ntfy_token"] = ntfy_token

    def done(res, err):
        if err:
            win.error("Couldn't save", err)
            on_done(False)
            return
        win.reload_all()
        on_done(True)
    win.helper.call("save_config", params, done)


class JobsPage(Adw.PreferencesPage):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.group = Adw.PreferencesGroup(title="Backup jobs",
                                          description="A job is what to back up, where it goes, and when.")
        add_job = text_button("", win.new_job, "flat")
        add_job.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Add Job"))
        self.group.set_header_suffix(add_job)
        self.add(self.group)
        self.rows: list = []
        self.refresh()

    def refresh(self) -> None:
        clear_group(self.group, self.rows)
        cfg = backend.load_config()
        if not cfg.jobs:
            r = Adw.ActionRow(title="No jobs yet", subtitle="Add one to start backing up.")
            self.group.add(r)
            self.rows.append(r)
        for name, job in cfg.jobs.items():
            sub = f"{job.schedule.describe() if job.enabled else 'Turned off'} · to {job.destination}"
            r = Adw.ActionRow(title=name, subtitle=GLib.markup_escape_text(sub))
            r.add_prefix(Gtk.Image.new_from_icon_name("document-open-recent-symbolic" if job.enabled else "process-stop-symbolic"))
            r.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            r.set_activatable(True)
            r.connect("activated", lambda _r, n=name: JobEditor(self.win, n).present())
            self.group.add(r)
            self.rows.append(r)


class DestinationsPage(Adw.PreferencesPage):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.group = Adw.PreferencesGroup(title="Destinations",
                                          description="Where backups are stored: a drive, a folder or a NAS share. "
                                                      "Several jobs can share one.")
        add = text_button("", lambda: DestEditor(win, None).present(), "flat")
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Add Destination"))
        self.group.set_header_suffix(add)
        self.add(self.group)
        self.rows: list = []
        self.refresh()

    def refresh(self) -> None:
        clear_group(self.group, self.rows)
        cfg = backend.load_config()
        if not cfg.destinations:
            r = Adw.ActionRow(title="No destinations yet",
                              subtitle="Add the drive, folder or NAS share to back up to.")
            self.group.add(r)
            self.rows.append(r)
        kinds = {t[0]: t[1] for t in DEST_TYPES}
        for name, d in cfg.destinations.items():
            used = [j.name for j in cfg.jobs.values() if j.destination == name]
            bits = [kinds.get(d.type, d.type), d.describe()]
            if d.encrypted:
                bits.append("encrypted")
            bits.append(f"used by {', '.join(used)}" if used else "not used by any job")
            r = Adw.ActionRow(title=name, subtitle=GLib.markup_escape_text(" · ".join(bits)))
            r.add_prefix(Gtk.Image.new_from_icon_name(DEST_ICONS.get(d.type, "folder-symbolic")))
            r.add_suffix(text_button("Test", lambda n=name: self._test(n), "flat"))
            r.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            r.set_activatable(True)
            r.connect("activated", lambda _r, n=name: DestEditor(self.win, n).present())
            self.group.add(r)
            self.rows.append(r)

    def _test(self, name: str) -> None:
        dest = backend.config_dict()["destinations"].get(name)
        if not dest:
            return
        self.win.toast(f"Testing {name}…")

        def done(res, err):
            if err:
                alert(self.win, f"Couldn't test {name}", err)
                return
            lines = [] if res.get("ok") else [res.get("message", "")]
            cap = res.get("capabilities")
            if cap:
                lines.append(f"{cap['fstype']} · {human_size(cap['free_bytes'])} free of "
                             f"{human_size(cap['total_bytes'])} · storage mode: {cap['mode']}")
                lines += cap.get("warnings", [])
            heading = "Ready for backups" if res.get("ok") else (
                "Not reachable" if not res.get("available") else "Can't use this destination")
            alert(self.win, f"{name}: {heading}", "\n\n".join(x for x in lines if x))
        self.win.helper.call("check_destination", {"destination": dest, "name": name}, done)


# ---------------------------------------------------------------------------

class ListEditor:
    """Rows of strings with remove buttons, inside a PreferencesGroup."""

    def __init__(self, group: Adw.PreferencesGroup, values: list[str], empty: str):
        self.group, self.values, self.empty = group, list(values), empty
        self.rows: list = []
        self.render()

    def render(self) -> None:
        clear_group(self.group, self.rows)
        if not self.values:
            r = Adw.ActionRow(title=self.empty, css_classes=["dim-label"])
            self.group.add(r)
            self.rows.append(r)
        for v in self.values:
            r = Adw.ActionRow(title=GLib.markup_escape_text(v))
            r.add_suffix(icon_button("list-remove-symbolic", "Remove", lambda v=v: self.remove(v)))
            self.group.add(r)
            self.rows.append(r)

    def add(self, v: str) -> None:
        v = v.strip()
        if v and v not in self.values:
            self.values.append(v)
            self.render()

    def remove(self, v: str) -> None:
        self.values.remove(v)
        self.render()


def spin(title: str, value: int, lo: int, hi: int, subtitle: str = "") -> Adw.SpinRow:
    r = Adw.SpinRow.new_with_range(lo, hi, 1)
    r.set_title(title)
    if subtitle:
        r.set_subtitle(subtitle)
    r.set_value(value)
    return r


class JobEditor:
    def __init__(self, win, name: str | None, preset: str | None = None):
        self.win = win
        self.cfg = backend.config_dict()
        self.is_new = name is None
        self.name = name
        if name:
            self.job = copy.deepcopy(self.cfg["jobs"][name])
        else:
            self.job = default_job_dict()
            self.job["modules"] = list(registry.PRESETS.get(preset or "essentials", []))
            self.job["schedule"]["mode"] = "daily"
            if self.cfg["destinations"]:
                self.job["destination"] = next(iter(self.cfg["destinations"]))
        page = Adw.PreferencesPage()
        self._general(page)
        self._what(page)
        self._schedule(page)
        self._retention(page)
        self._advanced(page)
        if not self.is_new:
            g = Adw.PreferencesGroup()
            delete = text_button("Delete Job", self._delete, "destructive-action")
            delete.set_halign(Gtk.Align.CENTER)
            g.add(delete)
            page.add(g)
        self.dlg, self.ok = dialog_shell("New Backup Job" if self.is_new else f"Job: {name}", page,
                                         ok_label="Create" if self.is_new else "Save", on_ok=self._save,
                                         width=680, height=780)

    def present(self):
        self.dlg.present(self.win)

    # -- sections ----------------------------------------------------------------
    def _general(self, page):
        g = Adw.PreferencesGroup(title="General")
        if self.is_new:
            self.name_row = Adw.EntryRow(title="Job name (lowercase, like daily or home)")
            self.name_row.set_text("daily" if "daily" not in self.cfg["jobs"] else "")
            g.add(self.name_row)
        self.desc_row = Adw.EntryRow(title="Description (optional)")
        self.desc_row.set_text(self.job.get("description", ""))
        g.add(self.desc_row)
        self.enabled_row = Adw.SwitchRow(title="Turned on", subtitle="When off, the job only runs when you start it",
                                         active=self.job.get("enabled", True))
        g.add(self.enabled_row)
        self.dest_names = list(self.cfg["destinations"])
        self.dest_row = Adw.ComboRow(title="Destination", model=Gtk.StringList.new(self.dest_names))
        if self.job.get("destination") in self.dest_names:
            self.dest_row.set_selected(self.dest_names.index(self.job["destination"]))
        self.dest_row.add_suffix(icon_button("list-add-symbolic", "Add another destination", self._add_dest))
        self.dest_row.connect("notify::selected", lambda *_: self._update_drive_trigger())
        g.add(self.dest_row)
        # First job ever: no destinations yet, so offer to create one right here.
        self.no_dest_row = Adw.ActionRow(title="Destination",
                                         subtitle="Where backups are stored. None set up yet.")
        self.no_dest_row.add_suffix(text_button("Add Destination", self._add_dest, "suggested-action"))
        g.add(self.no_dest_row)
        self.dest_row.set_visible(bool(self.dest_names))
        self.no_dest_row.set_visible(not self.dest_names)
        page.add(g)

    def _add_dest(self) -> None:
        DestEditor(self.win, None, on_saved=self._dest_added).present()

    def _dest_added(self, name: str) -> None:
        fresh = backend.config_dict()
        self.cfg["destinations"] = fresh["destinations"]
        self.dest_names = list(fresh["destinations"])
        self.dest_row.set_model(Gtk.StringList.new(self.dest_names))
        self.dest_row.set_visible(True)
        self.no_dest_row.set_visible(False)
        if name in self.dest_names:
            self.dest_row.set_selected(self.dest_names.index(name))
        self._update_drive_trigger()

    def _what(self, page):
        g = Adw.PreferencesGroup(title="What to back up")
        presets = Gtk.Box(spacing=6)
        for key, label in (("essentials", "Essentials"), ("home", "Home"), ("everything", "Everything")):
            b = text_button(label, lambda k=key: self._apply_preset(k), "flat")
            b.set_tooltip_text(", ".join(registry.get(m).name for m in registry.PRESETS[key]))
            presets.append(b)
        g.set_header_suffix(presets)
        self.module_rows: dict[str, Adw.SwitchRow] = {}
        for m in registry.describe():
            sub = m["summary"]
            if m["sensitive"]:
                sub += " Needs an encrypted destination."
            r = Adw.SwitchRow(title=m["name"], subtitle=GLib.markup_escape_text(sub),
                              active=m["id"] in self.job["modules"])
            r.set_subtitle_lines(3)
            self.module_rows[m["id"]] = r
            g.add(r)
        page.add(g)

        users = [u.name for u in system.regular_users()]
        ug = Adw.PreferencesGroup(title="Users", description="Whose home folders the per-user items cover. "
                                                             "With none ticked, everyone is included (also future users).")
        self.user_rows: dict[str, Adw.SwitchRow] = {}
        for u in users:
            r = Adw.SwitchRow(title=u, active=u in self.job["users"])
            self.user_rows[u] = r
            ug.add(r)
        if not users:
            ug.add(Adw.ActionRow(title="No regular users found"))
        page.add(ug)

        inc = Adw.PreferencesGroup(title="Extra folders and files",
                                   description="Anything else to include, like /srv or a games save folder.")
        add = text_button("", self._add_include, "flat")
        add.set_child(Adw.ButtonContent(icon_name="list-add-symbolic", label="Add Folder"))
        inc.set_header_suffix(add)
        self.includes = ListEditor(inc, self.job["include"], "None")
        page.add(inc)

        exc = Adw.PreferencesGroup(title="Skip these",
                                   description="rsync patterns. /home/*/Videos skips everyone's Videos folder; "
                                               "*.iso skips ISO files anywhere.")
        self.exclude_entry = Adw.EntryRow(title="Add a pattern", show_apply_button=True)
        self.exclude_entry.connect("apply", lambda r: (self.excludes.add(r.get_text()), r.set_text("")))
        exc.add(self.exclude_entry)
        self.excludes = ListEditor(exc, self.job["exclude"], "Nothing extra (caches and trash are always skipped)")
        page.add(exc)

    def _apply_preset(self, key: str) -> None:
        for mid, row in self.module_rows.items():
            row.set_active(mid in registry.PRESETS[key])

    def _add_include(self) -> None:
        fd = Gtk.FileDialog(title="Add a folder to the backup")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            if f and f.get_path():
                self.includes.add(f.get_path())
        fd.select_folder(self.win, None, done)

    def _schedule(self, page):
        sc = self.job["schedule"]
        g = Adw.PreferencesGroup(title="When")
        self.mode_row = Adw.ComboRow(title="Schedule", model=Gtk.StringList.new([m[1] for m in MODES]))
        self.mode_row.set_selected([m[0] for m in MODES].index(sc["mode"]) if sc["mode"] in dict(MODES) else 0)
        self.mode_row.connect("notify::selected", lambda *_: self._update_schedule())
        g.add(self.mode_row)

        hh, mm = (int(x) for x in sc["time"].split(":"))
        self.time_row = Adw.ActionRow(title="Time")
        self.hour = Gtk.SpinButton.new_with_range(0, 23, 1)
        self.minute = Gtk.SpinButton.new_with_range(0, 59, 1)
        for sb, v in ((self.hour, hh), (self.minute, mm)):
            sb.set_value(v)
            sb.set_valign(Gtk.Align.CENTER)
            sb.set_numeric(True)
            sb.connect("output", self._two_digits)
        box = Gtk.Box(spacing=4, valign=Gtk.Align.CENTER)
        box.append(self.hour)
        box.append(Gtk.Label(label=":"))
        box.append(self.minute)
        self.time_row.add_suffix(box)
        g.add(self.time_row)

        self.day_row = Adw.ComboRow(title="Day", model=Gtk.StringList.new([d[1] for d in DAYS]))
        self.day_row.set_selected([d[0] for d in DAYS].index(sc["weekday"]) if sc["weekday"] in dict(DAYS) else 6)
        g.add(self.day_row)
        self.mday_row = spin("Day of the month", sc["day"], 1, 28)
        g.add(self.mday_row)
        self.cal_row = Adw.EntryRow(title="systemd calendar expression, like Mon..Fri 18:00", show_apply_button=True)
        self.cal_row.set_text(sc.get("calendar", ""))
        self.cal_row.connect("apply", self._check_calendar)
        g.add(self.cal_row)
        self.delay_row = spin("Random delay (minutes)", sc.get("random_delay_min", 0), 0, 1440,
                              "Spreads start times, useful when many PCs share a NAS")
        g.add(self.delay_row)

        tr = self.job["triggers"]
        self.pacman_row = Adw.SwitchRow(title="Also before pacman changes packages",
                                        subtitle="A safety snapshot before every upgrade. Best with small, quick jobs.",
                                        active=tr["before_pacman"])
        g.add(self.pacman_row)
        self.plug_row = Adw.SwitchRow(title="Also when the drive is plugged in", active=tr["on_drive_connect"])
        g.add(self.plug_row)
        self.gap_row = spin("Minimum hours between these extra runs", tr["min_interval_hours"], 0, 720,
                            "0 means every time")
        g.add(self.gap_row)
        page.add(g)
        self._update_schedule()
        self._update_drive_trigger()

    @staticmethod
    def _two_digits(sb):
        sb.set_text(f"{int(sb.get_value()):02d}")
        return True

    def _update_schedule(self):
        mode = MODES[self.mode_row.get_selected()][0]
        self.time_row.set_visible(mode in ("hourly", "daily", "weekly", "monthly"))
        self.hour.set_sensitive(mode != "hourly")
        self.time_row.set_subtitle("Minutes past each hour" if mode == "hourly" else "")
        self.day_row.set_visible(mode == "weekly")
        self.mday_row.set_visible(mode == "monthly")
        self.cal_row.set_visible(mode == "custom")
        self.delay_row.set_visible(mode != "manual")

    def _update_drive_trigger(self):
        idx = self.dest_row.get_selected()
        d = self.cfg["destinations"].get(self.dest_names[idx]) if 0 <= idx < len(self.dest_names) else None
        is_drive = bool(d and d.get("type") == "drive")
        self.plug_row.set_sensitive(is_drive)
        self.plug_row.set_subtitle("" if is_drive else "Only for drive destinations")
        if not is_drive:
            self.plug_row.set_active(False)

    def _check_calendar(self, row):
        try:
            nxt = schedule.check_calendar(row.get_text().strip())
            self.win.toast(f"Next run: {nxt}" if nxt else "Looks valid")
        except LifeboatError as exc:
            alert(self.win, "Invalid schedule", str(exc))

    def _retention(self, page):
        r = self.job["retention"]
        g = Adw.PreferencesGroup(title="How many to keep",
                                 description="Kept: the newest few, plus one per hour, day, week, month and year "
                                             "for as many as you set. Unchanged files are shared, so extra "
                                             "backups usually cost little space.")
        self.keep_rows = {}
        for key, title in (("keep_last", "Newest backups"), ("keep_hourly", "Hourly"), ("keep_daily", "Daily"),
                           ("keep_weekly", "Weekly"), ("keep_monthly", "Monthly"), ("keep_yearly", "Yearly"),
                           ("keep_within_days", "Everything from the last … days")):
            row = spin(title, r[key], 0, 9999)
            self.keep_rows[key] = row
            g.add(row)
        self.keep_all = Adw.SwitchRow(title="Never remove backups", active=r["keep_all"])
        g.add(self.keep_all)
        self.min_free = spin("Keep this much space free (GiB)", r["min_free_gb"], 0, 100000,
                             "Removes the oldest backups when the destination gets this full. 0 = off")
        g.add(self.min_free)
        self.prune_after = Adw.SwitchRow(title="Clean up after each backup", active=r["prune_after_backup"])
        g.add(self.prune_after)
        page.add(g)

    def _advanced(self, page):
        g = Adw.PreferencesGroup(title="Advanced")
        exp = Adw.ExpanderRow(title="Advanced options", subtitle="Commands, health checks and safety switches")
        self.onefs = Adw.SwitchRow(title="Stay on the same filesystem",
                                   subtitle="Skip other drives mounted inside the backed-up folders",
                                   active=self.job["one_file_system"])
        self.secrets = Adw.SwitchRow(title="Allow keys on unencrypted destinations",
                                     subtitle="Saves SSH/GPG keys and LUKS headers even if the destination isn't encrypted",
                                     active=self.job["allow_unencrypted_secrets"])
        self.lossy = Adw.SwitchRow(title="Allow destinations that lose file owners",
                                   subtitle="For shares that squash root. Restored files belong to the folder's owner.",
                                   active=self.job["allow_lossy"])
        self.pre = Adw.EntryRow(title="Command before the backup (runs as root)")
        self.pre.set_text(self.job["pre_command"])
        self.post = Adw.EntryRow(title="Command after the backup ($LIFEBOAT_STATUS is set)")
        self.post.set_text(self.job["post_command"])
        self.hc = Adw.EntryRow(title="Health check URL (healthchecks.io style)")
        self.hc.set_text(self.job["healthcheck_url"])
        for w in (self.onefs, self.secrets, self.lossy, self.pre, self.post, self.hc):
            exp.add_row(w)
        g.add(exp)
        page.add(g)

    # -- save ----------------------------------------------------------------------
    def _collect(self) -> dict:
        j = self.job
        j["description"] = self.desc_row.get_text().strip()
        j["enabled"] = self.enabled_row.get_active()
        idx = self.dest_row.get_selected()
        j["destination"] = self.dest_names[idx] if 0 <= idx < len(self.dest_names) else ""
        j["modules"] = [m for m, r in self.module_rows.items() if r.get_active()]
        j["users"] = [u for u, r in self.user_rows.items() if r.get_active()]
        j["include"] = list(self.includes.values)
        j["exclude"] = list(self.excludes.values)
        sc = j["schedule"]
        sc["mode"] = MODES[self.mode_row.get_selected()][0]
        sc["time"] = f"{int(self.hour.get_value()):02d}:{int(self.minute.get_value()):02d}"
        sc["weekday"] = DAYS[self.day_row.get_selected()][0]
        sc["day"] = int(self.mday_row.get_value())
        sc["calendar"] = self.cal_row.get_text().strip()
        sc["random_delay_min"] = int(self.delay_row.get_value())
        tr = j["triggers"]
        tr["before_pacman"] = self.pacman_row.get_active()
        tr["on_drive_connect"] = self.plug_row.get_active()
        tr["min_interval_hours"] = int(self.gap_row.get_value())
        r = j["retention"]
        for k, row in self.keep_rows.items():
            r[k] = int(row.get_value())
        r["keep_all"] = self.keep_all.get_active()
        r["min_free_gb"] = int(self.min_free.get_value())
        r["prune_after_backup"] = self.prune_after.get_active()
        j["one_file_system"] = self.onefs.get_active()
        j["allow_unencrypted_secrets"] = self.secrets.get_active()
        j["allow_lossy"] = self.lossy.get_active()
        j["pre_command"] = self.pre.get_text()
        j["post_command"] = self.post.get_text()
        j["healthcheck_url"] = self.hc.get_text().strip()
        return j

    def _save(self) -> None:
        job = self._collect()
        fresh = backend.config_dict()
        name = self.name
        if self.is_new:
            name = self.name_row.get_text().strip().lower()
            if not config.NAME_RE.match(name or ""):
                alert(self.win, "Pick a job name", "Use lowercase letters, digits, - or _ (up to 32), like daily.")
                return
            if name in fresh["jobs"]:
                alert(self.win, "Name taken", f"There's already a job called '{name}'.")
                return
        if not job["destination"]:
            alert(self.win, "Choose a destination", "Add a destination with the + button next to Destination.")
            return
        fresh["jobs"][name] = job
        self.ok.set_sensitive(False)

        def done(ok):
            self.ok.set_sensitive(True)
            if ok:
                self.dlg.close()
                self.win.toast(f"Job '{name}' saved")
        save_config(self.win, fresh, done)

    def _delete(self) -> None:
        def go():
            fresh = backend.config_dict()
            fresh["jobs"].pop(self.name, None)

            def done(ok):
                if ok:
                    self.dlg.close()
                    self.win.toast(f"Job '{self.name}' deleted. Its backups were kept on the destination.")
            save_config(self.win, fresh, done)
        confirm(self.win, f"Delete job '{self.name}'?",
                "The job and its schedule are removed. Backups it already made stay on the destination; "
                "delete them from the Backups page first if you want them gone.", "Delete Job", go)


# ---------------------------------------------------------------------------

class DestEditor:
    def __init__(self, win, name: str | None, on_saved=None):
        self.win = win
        self.on_saved = on_saved
        self.cfg = backend.config_dict()
        self.is_new = name is None
        self.name = name
        self.d = copy.deepcopy(self.cfg["destinations"][name]) if name else {"type": "drive", "encrypted": False}
        self.drives: list[dict] = []

        page = Adw.PreferencesPage()
        g = Adw.PreferencesGroup()
        if self.is_new:
            self.name_row = Adw.EntryRow(title="Name (lowercase, like usb or nas)")
            g.add(self.name_row)
        types = [t[0] for t in DEST_TYPES]
        self.type_row = Adw.ComboRow(title="Type", model=Gtk.StringList.new([t[1] for t in DEST_TYPES]))
        self.type_row.set_selected(types.index(self.d.get("type", "drive")))
        self.type_row.connect("notify::selected", lambda *_: self._update())
        g.add(self.type_row)
        page.add(g)

        # folder
        self.folder_group = Adw.PreferencesGroup(title="Folder")
        self.path_row = Adw.EntryRow(title="Folder path")
        self.path_row.set_text(self.d.get("path", ""))
        self.path_row.add_suffix(icon_button("folder-open-symbolic", "Choose a folder", self._choose_folder))
        self.folder_group.add(self.path_row)
        self.same_disk = Adw.SwitchRow(title="Allow the system's own disk",
                                       subtitle="Protects against mistakes, not against the disk failing",
                                       active=self.d.get("same_disk_ok", False))
        self.folder_group.add(self.same_disk)
        page.add(self.folder_group)

        # drive
        self.drive_group = Adw.PreferencesGroup(title="Drive",
                                                description="Use an ext4, btrfs or xfs drive. FAT and exFAT can't hold snapshots.")
        self.drive_group.set_header_suffix(icon_button("view-refresh-symbolic", "Look for drives again", self._load_drives))
        self.drive_row = Adw.ComboRow(title="Drive", model=Gtk.StringList.new(["Looking for drives…"]))
        self.drive_row.connect("notify::selected", lambda *_: self._drive_selected())
        self.drive_group.add(self.drive_row)
        self.keyfile_row = Adw.EntryRow(title="Key file to unlock it (optional)")
        self.keyfile_row.set_text(self.d.get("keyfile", ""))
        self.drive_group.add(self.keyfile_row)
        page.add(self.drive_group)

        # nfs
        self.nfs_group = Adw.PreferencesGroup(title="Network share")
        self.server_row = Adw.EntryRow(title="Server (name or IP)")
        self.server_row.set_text(self.d.get("server", ""))
        self.nfs_group.add(self.server_row)
        self.export_row = Adw.EntryRow(title="Shared folder on the server, like /mnt/pool/backups")
        self.export_row.set_text(self.d.get("export", ""))
        self.export_row.add_suffix(icon_button("system-search-symbolic", "List the server's shares", self._find_exports))
        self.nfs_group.add(self.export_row)
        self.options_row = Adw.EntryRow(title="Extra mount options (optional), like vers=4.2")
        self.options_row.set_text(self.d.get("options", ""))
        self.nfs_group.add(self.options_row)
        page.add(self.nfs_group)

        common = Adw.PreferencesGroup()
        self.subdir_row = Adw.EntryRow(title="Subfolder to use (optional)")
        self.subdir_row.set_text(self.d.get("subdir", ""))
        common.add(self.subdir_row)
        self.enc_row = Adw.SwitchRow(title="This storage is encrypted",
                                     subtitle="Allows keys, passwords and LUKS headers. Detected automatically for LUKS drives.",
                                     active=self.d.get("encrypted", False))
        common.add(self.enc_row)
        page.add(common)

        self.test_group = Adw.PreferencesGroup(title="Test", description="Checks the destination is reachable and "
                                               "what it can store. Mounts it briefly if needed.")
        self.test_btn = text_button("Test Destination", self._test)
        self.test_group.set_header_suffix(self.test_btn)
        self.test_rows: list = []
        page.add(self.test_group)

        if not self.is_new:
            dg = Adw.PreferencesGroup()
            delete = text_button("Delete Destination", self._delete, "destructive-action")
            delete.set_halign(Gtk.Align.CENTER)
            dg.add(delete)
            page.add(dg)

        self.dlg, self.ok = dialog_shell("New Destination" if self.is_new else f"Destination: {name}", page,
                                         ok_label="Add" if self.is_new else "Save", on_ok=self._save,
                                         width=620, height=760)
        self._update()
        self._load_drives()

    def present(self):
        self.dlg.present(self.win)

    def _type(self) -> str:
        return DEST_TYPES[self.type_row.get_selected()][0]

    def _update(self):
        t = self._type()
        self.folder_group.set_visible(t == "folder")
        self.drive_group.set_visible(t == "drive")
        self.nfs_group.set_visible(t == "nfs")
        self.subdir_row.set_visible(t != "folder")

    def _load_drives(self):
        try:
            self.drives = [d for d in destinations.list_drives() if d["usable"]]
        except Exception as exc:
            self.drives = []
            self.win.toast(f"Couldn't list drives: {exc}")
        labels = []
        for d in self.drives:
            bits = [d["label"] or d["model"] or d["path"], human_size(d["size"]), d["fstype"]]
            if d["encrypted"]:
                bits.append("encrypted")
            if d["system"]:
                bits.append("system disk")
            labels.append(" · ".join(b for b in bits if b))
        current = self.d.get("uuid")
        if current and not any(d["uuid"] == current for d in self.drives):
            self.drives.insert(0, {"uuid": current, "encrypted": False, "system": False})
            labels.insert(0, f"Not connected ({current[:13]})")
        self.drive_row.set_model(Gtk.StringList.new(labels or ["No usable drives found"]))
        self.drive_row.set_sensitive(bool(self.drives))
        if current:
            for i, d in enumerate(self.drives):
                if d["uuid"] == current:
                    self.drive_row.set_selected(i)
        self._drive_selected()

    def _drive_selected(self):
        d = self._drive()
        self.keyfile_row.set_visible(bool(d and d.get("encrypted")))
        if d and d.get("encrypted"):
            self.enc_row.set_active(True)
        if d and d.get("system"):
            self.drive_row.set_subtitle("This is a system disk: a failure would take the backups too.")
        elif d:
            self.drive_row.set_subtitle(GLib.markup_escape_text(f"UUID {d['uuid']}"))

    def _drive(self) -> dict | None:
        i = self.drive_row.get_selected()
        return self.drives[i] if 0 <= i < len(self.drives) else None

    def _choose_folder(self):
        fd = Gtk.FileDialog(title="Back up into this folder")

        def done(dlg, res):
            try:
                f = dlg.select_folder_finish(res)
            except GLib.Error:
                return
            self.path_row.set_text(f.get_path() or "")
        fd.select_folder(self.win, None, done)

    def _find_exports(self):
        server = self.server_row.get_text().strip()
        if not server:
            alert(self.win, "Enter the server first", "Type the NAS name or IP address, then look for its shares.")
            return

        def done(res, err):
            if err:
                alert(self.win, "Couldn't list shares", err)
                return
            if not res:
                alert(self.win, "No shares found", f"{server} doesn't export any NFS shares to this PC.")
                return
            d = Adw.AlertDialog(heading="Choose a share", body=f"Shares on {server}:")
            for i, e in enumerate(res[:12]):
                d.add_response(f"e{i}", e)
            d.add_response("cancel", "Cancel")
            d.set_close_response("cancel")
            d.connect("response", lambda _d, r: self.export_row.set_text(res[int(r[1:])]) if r.startswith("e") else None)
            d.present(self.win)
        self.win.helper.call("exports", {"server": server}, done)

    def _collect(self) -> dict:
        t = self._type()
        d = {"type": t, "encrypted": self.enc_row.get_active()}
        if t == "folder":
            d["path"] = self.path_row.get_text().strip()
            d["same_disk_ok"] = self.same_disk.get_active()
        elif t == "drive":
            drv = self._drive()
            d["uuid"] = drv["uuid"] if drv else ""
            d["keyfile"] = self.keyfile_row.get_text().strip()
            d["subdir"] = self.subdir_row.get_text().strip()
        else:
            d["server"] = self.server_row.get_text().strip()
            d["export"] = self.export_row.get_text().strip()
            d["options"] = self.options_row.get_text().strip()
            d["subdir"] = self.subdir_row.get_text().strip()
        return d

    def _test(self):
        clear_group(self.test_group, self.test_rows)
        self.test_btn.set_sensitive(False)
        self.test_btn.set_label("Testing…")
        name = self.name or (self.name_row.get_text().strip().lower() or "test")

        def done(res, err):
            self.test_btn.set_sensitive(True)
            self.test_btn.set_label("Test Destination")
            if err:
                rows = [Adw.ActionRow(title="Problem", subtitle=GLib.markup_escape_text(err))]
            else:
                ok = res.get("ok")
                head = Adw.ActionRow(title="Ready for backups" if ok else ("Not reachable" if not res.get("available") else "Can't use this"),
                                     subtitle=GLib.markup_escape_text(res.get("message", "")))
                img = Gtk.Image.new_from_icon_name("emblem-ok-symbolic" if ok else "dialog-error-symbolic")
                img.add_css_class("success" if ok else "error")
                head.add_prefix(img)
                rows = [head]
                cap = res.get("capabilities")
                if cap:
                    rows.append(Adw.ActionRow(title=f"{cap['fstype']} · {human_size(cap['free_bytes'])} free of {human_size(cap['total_bytes'])}",
                                              subtitle=f"Storage mode: {cap['mode']} · encrypted: {'yes' if cap['encrypted'] else 'no'}"))
                    for w in cap.get("warnings", []):
                        r = Adw.ActionRow(title="Note", subtitle=GLib.markup_escape_text(w))
                        r.set_subtitle_lines(4)
                        rows.append(r)
            for r in rows:
                self.test_group.add(r)
                self.test_rows.append(r)
        self.win.helper.call("check_destination", {"destination": self._collect(), "name": name}, done)

    def _save(self):
        fresh = backend.config_dict()
        name = self.name
        if self.is_new:
            name = self.name_row.get_text().strip().lower()
            if not config.NAME_RE.match(name or ""):
                alert(self.win, "Pick a name", "Use lowercase letters, digits, - or _ (up to 32), like usb or nas.")
                return
            if name in fresh["destinations"]:
                alert(self.win, "Name taken", f"There's already a destination called '{name}'.")
                return
        d = self._collect()
        if d["type"] == "drive" and not d["uuid"]:
            alert(self.win, "Choose a drive", "Plug the drive in and press the refresh button.")
            return
        fresh["destinations"][name] = d
        self.ok.set_sensitive(False)

        def done(ok):
            self.ok.set_sensitive(True)
            if ok:
                self.dlg.close()
                self.win.toast(f"Destination '{name}' saved")
                if self.on_saved:
                    self.on_saved(name)
        save_config(self.win, fresh, done)

    def _delete(self):
        fresh = backend.config_dict()
        users = [j for j, v in fresh["jobs"].items() if v.get("destination") == self.name]
        if users:
            alert(self.win, "Still in use", f"Jobs {', '.join(users)} back up here. Change or delete them first.")
            return

        def go():
            fresh["destinations"].pop(self.name, None)

            def done(ok):
                if ok:
                    self.dlg.close()
                    self.win.toast("Destination deleted. Backups stored there were not touched.")
            save_config(self.win, fresh, done)
        confirm(self.win, f"Delete destination '{self.name}'?",
                "Lifeboat forgets this destination. Backups already stored there are left alone.", "Delete", go)
