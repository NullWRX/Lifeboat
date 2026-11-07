"""Small UI helpers shared by the pages."""

from __future__ import annotations

import datetime
import time

from gi.repository import Adw, Gtk

from ..util import human_size

STATUS = {
    "success": ("emblem-ok-symbolic", "success", "Succeeded"),
    "warning": ("dialog-warning-symbolic", "warning", "Finished with warnings"),
    "failed": ("dialog-error-symbolic", "error", "Failed"),
    "skipped": ("dialog-information-symbolic", "dim-label", "Skipped"),
    "cancelled": ("process-stop-symbolic", "dim-label", "Cancelled"),
    "incomplete": ("dialog-warning-symbolic", "warning", "Incomplete"),
}

DEST_ICONS = {"folder": "folder-symbolic", "drive": "drive-removable-media-symbolic", "nfs": "folder-remote-symbolic"}


def status_icon(st: str | None) -> Gtk.Image:
    icon, css, label = STATUS.get(st or "", ("content-loading-symbolic", "dim-label", "Unknown"))
    img = Gtk.Image.new_from_icon_name(icon)
    img.add_css_class(css)
    img.set_tooltip_text(label)
    return img


def when(t) -> str:
    """Friendly relative time for past and future moments."""
    if not t:
        return "never"
    t = float(t)
    d = time.time() - t
    if -60 < d < 60:
        return "just now"
    if 0 < d < 3600:
        m = int(d // 60)
        return f"{m} minute{'s' if m != 1 else ''} ago"
    if 0 < d < 6 * 3600:
        h = int(d // 3600)
        return f"{h} hour{'s' if h != 1 else ''} ago"
    lt = time.localtime(t)
    days = (datetime.date(lt.tm_year, lt.tm_mon, lt.tm_mday) - datetime.date.today()).days
    hm = time.strftime("%H:%M", lt)
    if days == 0:
        return f"today at {hm}"
    if days == 1:
        return f"tomorrow at {hm}"
    if days == -1:
        return f"yesterday at {hm}"
    if -7 < days < 7:
        return time.strftime("%A at %H:%M", lt)
    return time.strftime("%d %b %Y", lt)


def full_time(t) -> str:
    return time.strftime("%a %d %b %Y, %H:%M", time.localtime(float(t))) if t else "-"


def size(n) -> str:
    return human_size(n) if n is not None else "?"


def icon_button(icon: str, tooltip: str, callback=None, flat: bool = True) -> Gtk.Button:
    b = Gtk.Button.new_from_icon_name(icon)
    b.set_tooltip_text(tooltip)
    b.set_valign(Gtk.Align.CENTER)
    if flat:
        b.add_css_class("flat")
    if callback:
        b.connect("clicked", lambda *_: callback())
    return b


def text_button(label: str, callback=None, css: str | None = None) -> Gtk.Button:
    b = Gtk.Button(label=label)
    b.set_valign(Gtk.Align.CENTER)
    if css:
        b.add_css_class(css)
    if callback:
        b.connect("clicked", lambda *_: callback())
    return b


def clear_group(group: Adw.PreferencesGroup, rows: list) -> None:
    for r in rows:
        group.remove(r)
    rows.clear()


def alert(parent, heading: str, body: str) -> None:
    d = Adw.AlertDialog(heading=heading, body=body)
    d.add_response("ok", "OK")
    d.present(parent)


def confirm(parent, heading: str, body: str, action: str, on_yes, destructive: bool = True) -> None:
    d = Adw.AlertDialog(heading=heading, body=body)
    d.add_response("cancel", "Cancel")
    d.add_response("yes", action)
    d.set_response_appearance("yes", Adw.ResponseAppearance.DESTRUCTIVE if destructive
                              else Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("cancel")
    d.set_close_response("cancel")
    d.connect("response", lambda _d, r: on_yes() if r == "yes" else None)
    d.present(parent)


def dialog_shell(title: str, content: Gtk.Widget, *, ok_label: str | None = None, on_ok=None,
                 width: int = 620, height: int = 720) -> tuple[Adw.Dialog, Gtk.Button | None]:
    """An Adw.Dialog with a header bar, Cancel and an optional primary button."""
    dlg = Adw.Dialog()
    dlg.set_title(title)
    dlg.set_content_width(width)
    dlg.set_content_height(height)
    tv = Adw.ToolbarView()
    hb = Adw.HeaderBar()
    ok = None
    if ok_label:
        hb.set_show_end_title_buttons(False)
        hb.set_show_start_title_buttons(False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: dlg.close())
        hb.pack_start(cancel)
        ok = Gtk.Button(label=ok_label)
        ok.add_css_class("suggested-action")
        if on_ok:
            ok.connect("clicked", lambda *_: on_ok())
        hb.pack_end(ok)
    tv.add_top_bar(hb)
    tv.set_content(content)
    dlg.set_child(tv)
    return dlg, ok


def busy_button(button: Gtk.Button, busy: bool, label: str | None = None) -> None:
    button.set_sensitive(not busy)
    if label is not None:
        button.set_label(label)


def wrap_label(text: str, css: list[str] | None = None, xalign: float = 0) -> Gtk.Label:
    lbl = Gtk.Label(label=text, wrap=True, xalign=xalign)
    lbl.set_wrap_mode(2)  # WORD_CHAR
    for c in css or []:
        lbl.add_css_class(c)
    return lbl
