"""Finding backups already stored at a destination, using them, and unlocking encrypted drives."""

from __future__ import annotations

from gi.repository import Adw, GLib, Gtk

from .. import config, system
from . import backend
from .widgets import alert, dialog_shell, text_button, when


def call_unlocking(win, method: str, params: dict, done) -> None:
    """Call the helper; if an encrypted drive is locked, ask for its passphrase and retry."""
    def cb(res, err):
        if err is not None and getattr(err, "locked", False):
            ask_passphrase(win, err.uuid, lambda: win.helper.call(method, params, cb), lambda: done(None, err))
        else:
            done(res, err)
    win.helper.call(method, params, cb)


def ask_passphrase(win, uuid: str, on_unlocked, on_cancel, error: str = "") -> None:
    d = Adw.AlertDialog(heading="Unlock the backup drive",
                        body=(error + "\n\n" if error else "") +
                        "This drive is encrypted. Enter its passphrase. It stays unlocked until you "
                        "lock it in your file manager or unplug it.")
    entry = Gtk.PasswordEntry(show_peek_icon=True, activates_default=True)
    d.set_extra_child(entry)
    d.add_response("cancel", "Cancel")
    d.add_response("unlock", "Unlock")
    d.set_response_appearance("unlock", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("unlock")
    d.set_close_response("cancel")

    def on_response(_d, resp):
        if resp != "unlock":
            on_cancel()
            return
        pw = entry.get_text()

        def done(res, err):
            if err:
                ask_passphrase(win, uuid, on_unlocked, on_cancel, error=str(err))
            else:
                win.toast("Drive unlocked")
                on_unlocked()
        win.helper.call("unlock_drive", {"uuid": uuid, "passphrase": pw}, done)
    d.connect("response", on_response)
    d.present(win)
    entry.grab_focus()


def this_host() -> str:
    cfg = backend.load_config()
    return (cfg.settings.hostname or system.hostname()).lower()


def find_backups(win, dest_name: str, quiet: bool = False) -> None:
    """Look for existing backups at a destination. quiet: say nothing if there are none."""
    def done(res, err):
        if err:
            if not quiet:
                alert(win, "Couldn't look for backups", str(err))
            return
        if not res["pcs"]:
            if not quiet:
                alert(win, "No backups found", f"There are no Lifeboat backups at {dest_name} yet.")
            return
        # Nothing new to offer when this PC's own jobs are already set up.
        cfg = backend.load_config()
        offer = [p for p in res["pcs"] if any(j["name"] not in cfg.jobs for j in p["jobs"])
                 or not (p["name"] == res["this_pc"] and p["this_machine"])]
        if not offer and quiet:
            return
        FoundDialog(win, dest_name, res).present()
    call_unlocking(win, "find_backups", {"destination": dest_name}, done)


class FoundDialog:
    def __init__(self, win, dest_name: str, res: dict):
        self.win, self.dest_name, self.res = win, dest_name, res
        cfg = backend.load_config()
        page = Adw.PreferencesPage()
        intro = Adw.PreferencesGroup(
            description=f"Lifeboat found backups at {GLib.markup_escape_text(dest_name)}. Each computer "
                        "keeps its backups in a folder with its name. To carry on one computer's backups "
                        "on this one (for example after reinstalling), pick the jobs you want and choose "
                        "Use These Backups.")
        page.add(intro)
        self.switches: dict[str, dict[str, Adw.SwitchRow]] = {}
        for pc in res["pcs"]:
            mine = pc["name"] == res["this_pc"]
            label = pc["name"] + (" · this PC's name" if mine else "")
            desc = f"Last backup {when(pc['newest'])}"
            if pc["hostname"] and pc["hostname"] != pc["name"]:
                desc += f" · computer called {pc['hostname']}"
            if pc["this_machine"]:
                desc += " · made by this computer"
            g = Adw.PreferencesGroup(title=GLib.markup_escape_text(label), description=GLib.markup_escape_text(desc))
            rows = {}
            for j in pc["jobs"]:
                sub = f"{j['snapshots']} backup{'s' if j['snapshots'] != 1 else ''}"
                if j["newest"]:
                    sub += f", newest {when(j['newest'])}"
                if j["modules"]:
                    sub += " · " + ", ".join(j["modules"])
                r = Adw.SwitchRow(title=j["name"], subtitle=GLib.markup_escape_text(sub), active=True)
                if j["name"] in cfg.jobs:
                    r.set_active(False)
                    r.set_sensitive(False)
                    r.set_subtitle("A job with this name is already set up")
                rows[j["name"]] = r
                g.add(r)
            if any(r.get_sensitive() for r in rows.values()):
                b = text_button("Use These Backups", lambda p=pc: self._use(p), "suggested-action")
                g.set_header_suffix(b)
            self.switches[pc["name"]] = rows
            page.add(g)
        self.dlg, _ = dialog_shell("Existing Backups", page, width=640, height=620)

    def present(self):
        self.dlg.present(self.win)

    def _use(self, pc: dict) -> None:
        jobs = [n for n, r in self.switches[pc["name"]].items() if r.get_active() and r.get_sensitive()]
        if not jobs:
            alert(self.win, "Pick at least one job", "Turn on the jobs you want to continue.")
            return
        here = self.res["this_pc"]
        if pc["name"] != here:
            d = Adw.AlertDialog(heading=f"Continue as '{pc['name']}'?",
                                body=f"This PC is called '{here}' in Lifeboat. To continue these backups, it "
                                     f"will save its backups as '{pc['name']}' from now on. You can rename it "
                                     "later on the Destinations tab.")
            d.add_response("cancel", "Cancel")
            d.add_response("go", "Continue")
            d.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
            d.set_close_response("cancel")
            d.connect("response", lambda _d, r: self._do(pc, jobs) if r == "go" else None)
            d.present(self.win)
        else:
            self._do(pc, jobs)

    def _do(self, pc: dict, jobs: list[str]) -> None:
        def done(res, err):
            if err:
                alert(self.win, "Couldn't use these backups", str(err))
                return
            self.dlg.close()
            self.win.reload_all()
            lines = []
            if res["imported"]:
                lines.append("Added: " + ", ".join(res["imported"]) + ".")
                lines.append("They start turned off, so backups of this system can't push out your old "
                             "backups before you've restored what you need. Restore on the Backups tab, "
                             "then turn the jobs on in Jobs.")
            lines += [f"Skipped {s}" for s in res["skipped"]]
            lines += res["notes"]
            alert(self.win, "Backups ready to use", "\n\n".join(lines))
            from .setup_page import handle_cleanup
            handle_cleanup(self.win, res.get("cleanup") or [])
        call_unlocking(self.win, "use_backups", {"destination": self.dest_name, "pc": pc["name"], "jobs": jobs}, done)


def rename_pc(win) -> None:
    cfg = backend.load_config()
    current = (cfg.settings.hostname or system.hostname()).lower()
    d = Adw.AlertDialog(heading="Rename this PC",
                        body="Backups are kept in a folder with this name at each destination. Renaming "
                             "moves those folders. Use lowercase letters, digits, - or _.")
    entry = Gtk.Entry(text=current, activates_default=True)
    d.set_extra_child(entry)
    d.add_response("cancel", "Cancel")
    d.add_response("rename", "Rename")
    d.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("rename")
    d.set_close_response("cancel")

    def on_response(_d, resp):
        if resp != "rename":
            return
        name = entry.get_text().strip().lower()
        if name == current:
            return
        if not config.NAME_RE.match(name):
            alert(win, "Not a valid name", "Use lowercase letters, digits, - or _ (up to 32).")
            return
        from .setup_page import save_config
        fresh = backend.config_dict()
        fresh["settings"]["hostname"] = "" if name == system.hostname() else name
        save_config(win, fresh, lambda ok: win.toast(f"This PC is now '{name}'") if ok else None)
    d.connect("response", on_response)
    d.present(win)
