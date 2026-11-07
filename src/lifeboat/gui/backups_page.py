"""Backups: list snapshots, browse their files, restore with a preview first."""

from __future__ import annotations

import os

from gi.repository import Adw, GLib, Gtk

from .. import restore as restore_mod
from . import backend
from .widgets import (alert, clear_group, confirm, dialog_shell, full_time, icon_button, size, status_icon,
                      text_button, when)

MAX_ROWS = 3000


class BackupsPage(Gtk.Stack):
    def __init__(self, win):
        super().__init__(transition_type=Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.win = win
        self.job: str | None = None
        self.snaps: list[dict] = []
        self.loaded_for: str | None = None

        # --- list view -------------------------------------------------------
        self.list_page = Adw.PreferencesPage()
        self.job_group = Adw.PreferencesGroup()
        self.job_row = Adw.ComboRow(title="Job")
        self.job_row.add_prefix(Gtk.Image.new_from_icon_name("document-open-recent-symbolic"))
        self.job_row.connect("notify::selected", self._on_job_changed)
        self.job_group.add(self.job_row)
        self.job_group.set_header_suffix(icon_button("view-refresh-symbolic", "Reload", self.load))
        self.list_page.add(self.job_group)

        self.snap_group = Adw.PreferencesGroup(title="Snapshots")
        self.snap_rows: list = []
        self.list_page.add(self.snap_group)

        self.locked = Adw.StatusPage(icon_name="channel-secure-symbolic", title="Backups are protected",
                                     description="Looking inside backups needs administrator access, "
                                                 "because they hold every user's files.")
        unlock = text_button("Show Backups", self.load, "suggested-action")
        unlock.add_css_class("pill")
        unlock.set_halign(Gtk.Align.CENTER)
        self.locked.set_child(unlock)

        self.message = Adw.StatusPage(icon_name="dialog-information-symbolic")
        self.no_jobs = Adw.StatusPage(icon_name="document-open-recent-symbolic", title="No jobs yet",
                                      description="Set up a backup job first.")

        self.add_named(self.locked, "locked")
        self.add_named(self.list_page, "list")
        self.add_named(self.message, "message")
        self.add_named(self.no_jobs, "nojobs")

        # --- snapshot view ---------------------------------------------------
        self.browser = SnapshotBrowser(self)
        self.add_named(self.browser, "snapshot")
        self.refresh_jobs()

    # -- jobs ------------------------------------------------------------------
    def refresh_jobs(self) -> None:
        names = list(backend.load_config().jobs)
        self._job_names = names
        self.job_row.set_model(Gtk.StringList.new(names))
        if not names:
            self.set_visible_child_name("nojobs")
            return
        if self.job not in names:
            self.job = names[0]
        self.job_row.set_selected(names.index(self.job))
        if not self.win.helper.unlocked:
            self.set_visible_child_name("locked")

    def select_job(self, name: str) -> None:
        self.refresh_jobs()
        if name in self._job_names:
            self.job = name
            self.job_row.set_selected(self._job_names.index(name))
        self.load()

    def _on_job_changed(self, *_):
        idx = self.job_row.get_selected()
        if 0 <= idx < len(getattr(self, "_job_names", [])):
            new = self._job_names[idx]
            if new != self.job:
                self.job = new
                if self.win.helper.unlocked:
                    self.load()

    # -- snapshots -------------------------------------------------------------
    def load(self) -> None:
        if not self.job:
            return
        clear_group(self.snap_group, self.snap_rows)
        self.snap_group.set_description("Loading…")
        if self.get_visible_child_name() not in ("list",):
            self.set_visible_child_name("list")
        job = self.job

        def done(res, err):
            if job != self.job:
                return
            if err:
                self.message.set_title("Can't open the backups")
                self.message.set_description(err)
                retry = text_button("Try Again", self.load, "pill")
                retry.set_halign(Gtk.Align.CENTER)
                self.message.set_child(retry)
                self.set_visible_child_name("message" if self.win.helper.unlocked else "locked")
                if not self.win.helper.unlocked:
                    self.win.toast(err)
                return
            self.snaps = res["snapshots"]
            self._fill(res)
        self.win.helper.call("list_snapshots", {"job": job}, done)

    def _fill(self, res: dict) -> None:
        clear_group(self.snap_group, self.snap_rows)
        snaps = res["snapshots"]
        self.snap_group.set_description(GLib.markup_escape_text(f"Stored in {res['location']}"))
        if not snaps:
            row = Adw.ActionRow(title="No backups yet", subtitle="Run the job to make the first one.")
            self.snap_group.add(row)
            self.snap_rows.append(row)
            return
        for s in snaps:
            st = s.get("stats") or {}
            bits = [s.get("trigger") or "manual", size(st.get("total_size"))]
            if st.get("transferred_size") is not None:
                bits.append(f"{size(st.get('transferred_size'))} new")
            if s.get("pinned"):
                bits.append("pinned" + (f": {s['note']}" if s.get("note") else ""))
            row = Adw.ActionRow(title=full_time(s["time"]), subtitle=GLib.markup_escape_text(" · ".join(bits)))
            row.add_prefix(status_icon(s["status"]))
            if s.get("pinned"):
                row.add_suffix(Gtk.Image.new_from_icon_name("view-pin-symbolic"))
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.set_activatable(True)
            row.connect("activated", lambda _r, snap=s: self.open_snapshot(snap))
            self.snap_group.add(row)
            self.snap_rows.append(row)

    def open_snapshot(self, snap: dict) -> None:
        self.browser.open(self.job, snap)
        self.set_visible_child_name("snapshot")

    def back_to_list(self, reload: bool = False) -> None:
        self.set_visible_child_name("list")
        if reload:
            self.load()


class SnapshotBrowser(Gtk.Box):
    def __init__(self, page: BackupsPage):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.page = page
        self.win = page.win
        self.job = ""
        self.snap: dict = {}
        self.path = "/"
        self.selected: set[str] = set()

        bar = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=8, margin_bottom=8)
        bar.append(icon_button("go-previous-symbolic", "Back to the list", lambda: page.back_to_list()))
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.title = Gtk.Label(xalign=0, css_classes=["heading"])
        self.subtitle = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], ellipsize=3)
        titles.append(self.title)
        titles.append(self.subtitle)
        bar.append(titles)
        self.pin_btn = icon_button("view-pin-symbolic", "Keep this backup forever", self._toggle_pin)
        bar.append(self.pin_btn)
        bar.append(icon_button("package-x-generic-symbolic", "Saved system info (package lists, disk layout)",
                               self._show_meta))
        bar.append(icon_button("user-trash-symbolic", "Delete this backup", self._delete))
        self.append(bar)
        self.append(Gtk.Separator())

        pathbar = Gtk.Box(spacing=6, margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        self.up_btn = icon_button("go-up-symbolic", "Up one folder", self._up)
        pathbar.append(self.up_btn)
        pathbar.append(icon_button("go-home-symbolic", "Top of the backup", lambda: self.navigate("/")))
        self.path_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=1, css_classes=["monospace"])
        pathbar.append(self.path_label)
        self.append(pathbar)

        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        self.listbox.set_margin_start(12)
        self.listbox.set_margin_end(12)
        self.listbox.set_margin_bottom(12)
        self.listbox.set_valign(Gtk.Align.START)
        clamp = Adw.Clamp(maximum_size=900, child=self.listbox)
        scroller = Gtk.ScrolledWindow(vexpand=True, child=clamp)
        self.append(scroller)

        action = Gtk.ActionBar()
        self.sel_label = Gtk.Label(css_classes=["dim-label"])
        action.pack_start(self.sel_label)
        self.clear_btn = text_button("Clear", self._clear_selection, "flat")
        action.pack_start(self.clear_btn)
        self.restore_btn = text_button("Restore…", self._restore, "suggested-action")
        action.pack_end(self.restore_btn)
        self.append(action)

    def open(self, job: str, snap: dict) -> None:
        self.job, self.snap = job, snap
        self.selected.clear()
        self.title.set_label(full_time(snap["time"]))
        st = snap.get("stats") or {}
        self.subtitle.set_label(f"{job} · {size(st.get('total_size'))} · {', '.join(snap.get('modules') or []) or 'extra folders'}")
        self._update_pin()
        self.navigate("/")
        self._update_selection()

    def _update_pin(self) -> None:
        pinned = self.snap.get("pinned")
        self.pin_btn.set_tooltip_text("Allow removing this backup again" if pinned else "Keep this backup forever")
        if pinned:
            self.pin_btn.add_css_class("accent")
        else:
            self.pin_btn.remove_css_class("accent")

    def navigate(self, path: str) -> None:
        self.path = path or "/"
        self.path_label.set_label(self.path)
        self.up_btn.set_sensitive(self.path != "/")
        self._clear_rows()
        loading = Adw.ActionRow(title="Loading…")
        self.listbox.append(loading)

        def done(items, err):
            self._clear_rows()
            if err:
                self.listbox.append(Adw.ActionRow(title="Can't open this folder", subtitle=GLib.markup_escape_text(err)))
                return
            if not items:
                self.listbox.append(Adw.ActionRow(title="Empty folder"))
            for item in items[:MAX_ROWS]:
                self.listbox.append(self._row(item))
            if len(items) > MAX_ROWS:
                self.listbox.append(Adw.ActionRow(title=f"{len(items) - MAX_ROWS} more items not shown",
                                                  subtitle="Restore the whole folder, or use the command line."))
        self.win.helper.call("browse", {"job": self.job, "snapshot": self.snap["name"], "path": self.path}, done)

    def _clear_rows(self) -> None:
        while (child := self.listbox.get_first_child()) is not None:
            self.listbox.remove(child)

    def _row(self, item: dict) -> Gtk.Widget:
        row = Adw.ActionRow(title=GLib.markup_escape_text(item["name"]))
        check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
        check.set_active(item["path"] in self.selected or any(item["path"].startswith(s + "/") for s in self.selected))
        check.set_tooltip_text("Select to restore")
        check.connect("toggled", self._on_check, item["path"])
        icon = {"dir": "folder-symbolic", "link": "insert-link-symbolic"}.get(item["kind"], "text-x-generic-symbolic")
        row.add_prefix(Gtk.Image.new_from_icon_name(icon))
        row.add_prefix(check)  # prefixes are prepended: the checkbox ends up leftmost
        sub = when(item["mtime"])
        if item["kind"] == "file":
            sub = f"{size(item['size'])} · {sub}"
        elif item["kind"] == "link":
            sub = f"→ {item.get('target', '?')}"
        row.set_subtitle(GLib.markup_escape_text(sub))
        if item["kind"] == "dir":
            row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
            row.set_activatable(True)
            row.connect("activated", lambda *_: self.navigate(item["path"]))
        else:
            row.set_activatable_widget(check)
        return row

    def _on_check(self, check: Gtk.CheckButton, path: str) -> None:
        if check.get_active():
            self.selected.add(path)
        else:
            self.selected.discard(path)
        self._update_selection()

    def _update_selection(self) -> None:
        n = len(self.selected)
        self.sel_label.set_label(f"{n} selected" if n else "Select files or folders to restore")
        self.restore_btn.set_sensitive(n > 0)
        self.clear_btn.set_visible(n > 0)

    def _clear_selection(self) -> None:
        self.selected.clear()
        self.navigate(self.path)
        self._update_selection()

    def _up(self) -> None:
        self.navigate(os.path.dirname(self.path.rstrip("/")) or "/")

    # -- snapshot actions --------------------------------------------------------
    def _toggle_pin(self) -> None:
        pinned = not self.snap.get("pinned")

        def done(r, e):
            if e:
                self.win.error("Couldn't change the pin", e)
                return
            self.snap["pinned"] = pinned
            self._update_pin()
            self.win.toast("Pinned: pruning will keep this backup" if pinned else "Unpinned")
            self.page.load()
            self.page.set_visible_child_name("snapshot")
        self.win.helper.call("pin", {"job": self.job, "snapshot": self.snap["name"], "pinned": pinned}, done)

    def _delete(self) -> None:
        def go():
            def done(r, e):
                if e:
                    self.win.error("Couldn't delete the backup", e)
                    return
                self.win.toast("Backup deleted")
                self.page.back_to_list(reload=True)
            self.win.helper.call("delete_snapshot", {"job": self.job, "snapshot": self.snap["name"]}, done)
        confirm(self.win, "Delete this backup?",
                f"The backup from {full_time(self.snap['time'])} will be removed for good. Files shared "
                "with other backups stay in those backups.", "Delete", go)

    def _show_meta(self) -> None:
        MetaDialog(self.win, self.job, self.snap).present(self.win)

    def _restore(self) -> None:
        RestoreDialog(self.win, self.job, self.snap, sorted(self.selected)).present(self.win)


class MetaDialog:
    """Browse the metadata saved with a snapshot (package lists, disk layout...)."""

    def __init__(self, win, job: str, snap: dict):
        self.win, self.job, self.snap = win, job, snap
        self.nav = Adw.NavigationView()
        self.dlg, _ = dialog_shell("Saved system info", self.nav, width=720, height=620)
        self._push_dir("/")

    def present(self, parent):
        self.dlg.present(parent)

    def _push_dir(self, path: str) -> None:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(description="Files Lifeboat saved alongside this backup, such as "
                                                 "package lists with a reinstall script, and the disk layout.")
        page.add(group)
        nav_page = Adw.NavigationPage(title=os.path.basename(path) or "Saved system info", child=page)

        def done(items, err):
            if err:
                group.add(Adw.ActionRow(title="Nothing saved", subtitle=GLib.markup_escape_text(err)))
                return
            if not items:
                group.add(Adw.ActionRow(title="Nothing saved with this backup"))
            for it in items:
                row = Adw.ActionRow(title=GLib.markup_escape_text(it["name"]))
                row.add_prefix(Gtk.Image.new_from_icon_name("folder-symbolic" if it["kind"] == "dir" else "text-x-generic-symbolic"))
                if it["kind"] == "file":
                    row.set_subtitle(size(it["size"]))
                row.add_suffix(Gtk.Image.new_from_icon_name("go-next-symbolic"))
                row.set_activatable(True)
                if it["kind"] == "dir":
                    row.connect("activated", lambda *_, p=it["path"]: self._push_dir(p))
                else:
                    row.connect("activated", lambda *_, p=it["path"]: self._push_file(p))
                group.add(row)
        self.win.helper.call("browse", {"job": self.job, "snapshot": self.snap["name"], "path": path, "area": "meta"}, done)
        if path == "/" and not self.nav.get_visible_page():
            self.nav.add(nav_page)
        else:
            self.nav.push(nav_page)

    def _push_file(self, path: str) -> None:
        view = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.NONE,
                            left_margin=12, right_margin=12, top_margin=12, bottom_margin=12)
        scroller = Gtk.ScrolledWindow(vexpand=True, child=view)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(scroller)
        bar = Gtk.ActionBar()
        copy = text_button("Copy", None)
        save = text_button("Save As…", None, "suggested-action")
        bar.pack_end(save)
        bar.pack_end(copy)
        box.append(bar)
        self.nav.push(Adw.NavigationPage(title=os.path.basename(path), child=box))

        def done(text, err):
            text = err if err else text
            view.get_buffer().set_text(text)
            copy.connect("clicked", lambda *_: (view.get_clipboard().set(text), self.win.toast("Copied")))
            save.connect("clicked", lambda *_: self._save_as(os.path.basename(path), text))
        self.win.helper.call("read_meta", {"job": self.job, "snapshot": self.snap["name"], "path": path}, done)

    def _save_as(self, name: str, text: str) -> None:
        fd = Gtk.FileDialog(initial_name=name)

        def done(d, res):
            try:
                f = d.save_finish(res)
            except GLib.Error:
                return
            try:
                with open(f.get_path(), "w", encoding="utf-8") as fh:
                    fh.write(text)
                if name.endswith(".sh"):
                    os.chmod(f.get_path(), 0o755)
                self.win.toast(f"Saved {os.path.basename(f.get_path())}")
            except OSError as exc:
                alert(self.win, "Couldn't save", str(exc))
        fd.save(self.win, None, done)


class RestoreDialog:
    def __init__(self, win, job: str, snap: dict, items: list[str]):
        self.win, self.job, self.snap, self.items = win, job, snap, items
        self.target: str | None = None
        self.previewed = False

        page = Adw.PreferencesPage()
        what = Adw.PreferencesGroup(title="Restoring", description=f"From the backup of {full_time(snap['time'])}")
        exp = Adw.ExpanderRow(title=f"{len(items)} item{'s' if len(items) != 1 else ''}",
                              subtitle=GLib.markup_escape_text(", ".join(os.path.basename(i) or i for i in items[:4])
                                                               + ("…" if len(items) > 4 else "")))
        for i in items[:200]:
            exp.add_row(Adw.ActionRow(title=GLib.markup_escape_text(i), css_classes=["monospace"]))
        what.add(exp)
        page.add(what)

        opts = Adw.PreferencesGroup(title="Options")
        self.where = Adw.ComboRow(title="Restore to", model=Gtk.StringList.new(["Original location", "Another folder"]))
        self.where.connect("notify::selected", self._changed)
        opts.add(self.where)
        self.folder_row = Adw.ActionRow(title="Folder", subtitle="Not chosen")
        self.folder_row.add_suffix(text_button("Choose…", self._choose))
        self.folder_row.set_visible(False)
        opts.add(self.folder_row)
        self.exact = Adw.SwitchRow(title="Make folders match the backup exactly",
                                   subtitle="Also removes files that were added after this backup")
        self.exact.connect("notify::active", self._changed)
        opts.add(self.exact)
        self.force = Adw.SwitchRow(title="Allow system folders",
                                   subtitle="Needed to restore /usr, /boot and similar over the running system. Risky.")
        self.force.connect("notify::active", self._changed)
        # Only offer the risky switch when it matters.
        self.force.set_visible(any(i == "/" or any(i == c or i.startswith(c + "/") for c in restore_mod.CRITICAL)
                                   for i in items))
        opts.add(self.force)
        page.add(opts)

        self.result = Adw.PreferencesGroup(title="Preview")
        self.result_rows: list = []
        self.result.set_description("Check what will change before restoring.")
        page.add(self.result)

        self.dlg, self.ok = dialog_shell("Restore", page, ok_label="Preview", on_ok=self._ok, width=640, height=700)
        self._changed()

    def present(self, parent):
        self.dlg.present(parent)

    def _changed(self, *_):
        original = self.where.get_selected() == 0
        self.folder_row.set_visible(not original)
        self.where.set_subtitle("Files it replaces are kept in an undo folder" if original
                                else "Restored with their full paths inside the folder")
        self.force.set_visible(self.force.get_visible() and original)
        self.previewed = False
        self.ok.set_label("Preview")
        self.ok.remove_css_class("destructive-action")
        self.ok.add_css_class("suggested-action")
        self.ok.set_sensitive(self.where.get_selected() == 0 or bool(self.target))

    def _choose(self) -> None:
        fd = Gtk.FileDialog(title="Restore into this folder")

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            self.target = f.get_path()
            self.folder_row.set_subtitle(GLib.markup_escape_text(self.target))
            self._changed()
        fd.select_folder(self.win, None, done)

    def _params(self) -> dict:
        return {"job": self.job, "snapshot": self.snap["name"], "items": self.items,
                "target": self.target if self.where.get_selected() == 1 else None,
                "delete": self.exact.get_active(), "force": self.force.get_active()}

    def _ok(self) -> None:
        if not self.previewed:
            self._preview()
        else:
            self._start()

    def _preview(self) -> None:
        self.ok.set_sensitive(False)
        self.ok.set_label("Checking…")
        clear_group(self.result, self.result_rows)

        def done(res, err):
            self.ok.set_sensitive(True)
            if err:
                self.ok.set_label("Preview")
                self.result.set_description(GLib.markup_escape_text(err))
                return
            c = res["changes"]["counts"]
            total = c["new"] + c["changed"] + c["deleted"] + c["attributes"]
            self.result.set_description("Nothing would change: these files already match the backup." if total == 0 else
                                        f"{c['new']} new · {c['changed']} replaced · {c['deleted']} removed · "
                                        f"{c['attributes']} with other permissions or times")
            if total:
                exp = Adw.ExpanderRow(title="Changes", subtitle="Paths that will be written or removed")
                for it in res["changes"]["items"][:300]:
                    exp.add_row(Adw.ActionRow(title=GLib.markup_escape_text(it["path"]), subtitle=it["change"]))
                if res["changes"].get("truncated") or len(res["changes"]["items"]) > 300:
                    exp.add_row(Adw.ActionRow(title="…and more"))
                self.result.add(exp)
                self.result_rows.append(exp)
            self.previewed = True
            self.ok.set_label("Restore")
            original = self.where.get_selected() == 0
            if original and (c["changed"] or c["deleted"]):
                self.ok.remove_css_class("suggested-action")
                self.ok.add_css_class("destructive-action")
        self.win.helper.call("restore_preview", self._params(), done)

    def _start(self) -> None:
        self.ok.set_sensitive(False)

        def done(res, err):
            if err:
                self.ok.set_sensitive(True)
                self.win.error("Couldn't start the restore", err)
                return
            self.dlg.close()
            self.win.toast("Restore started. Progress shows on the Overview page.")
        self.win.helper.call("start_restore", self._params(), done)
