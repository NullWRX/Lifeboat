"""Mikoshi's neon look: a dark cyberpunk palette over libadwaita, switchable in Preferences.

The setting is per user (~/.config/mikoshi/gui.json), since it's only about how
the app looks. libadwaita 1.6 moved from named colors (@define-color) to CSS
variables, so both forms are provided and the right one is picked at runtime.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from gi.repository import Adw, Gdk, Gtk

CYAN = "#00e5ff"
MAGENTA = "#ff2bd6"
YELLOW = "#f5e900"
GREEN = "#00ff9c"
RED = "#ff4d7d"

PALETTE = {
    "window_bg_color": "#07080f", "window_fg_color": "#d6f6ff",
    "view_bg_color": "#0a0c17", "view_fg_color": "#d6f6ff",
    "headerbar_bg_color": "#080a14", "headerbar_fg_color": "#d6f6ff",
    "headerbar_border_color": MAGENTA, "headerbar_backdrop_color": "#07080f",
    "card_bg_color": "#0e1124", "card_fg_color": "#d6f6ff",
    "dialog_bg_color": "#0c0f20", "dialog_fg_color": "#d6f6ff",
    "popover_bg_color": "#0e1124", "popover_fg_color": "#d6f6ff",
    "sidebar_bg_color": "#0a0c17", "sidebar_fg_color": "#d6f6ff",
    "accent_bg_color": "#00d4ee", "accent_fg_color": "#00141a", "accent_color": "#3ff0ff",
    "destructive_bg_color": "#ff2b6d", "destructive_fg_color": "#12000a", "destructive_color": "#ff5c8d",
    "success_bg_color": "#00b874", "success_fg_color": "#001a10", "success_color": GREEN,
    "warning_bg_color": "#c9be00", "warning_fg_color": "#1a1700", "warning_color": YELLOW,
    "error_bg_color": "#ff2b6d", "error_fg_color": "#12000a", "error_color": RED,
}

MONO = '"JetBrains Mono", "Fira Code", "Source Code Pro", "Hack", "DejaVu Sans Mono", monospace'

RULES = f"""
window.background {{
  background-image:
    linear-gradient(alpha({CYAN}, 0.035) 1px, transparent 1px),
    linear-gradient(90deg, alpha({CYAN}, 0.035) 1px, transparent 1px);
  background-size: 32px 32px;
}}
headerbar {{
  border-bottom: 1px solid alpha({MAGENTA}, 0.45);
  box-shadow: 0 1px 14px alpha({MAGENTA}, 0.18);
}}
.boxed-list, list.boxed-list {{
  border: 1px solid alpha({CYAN}, 0.28);
  box-shadow: 0 0 14px alpha({CYAN}, 0.08);
}}
preferencesgroup .heading, .heading {{
  font-family: {MONO};
  letter-spacing: 1px;
}}
preferencesgroup > box > box .heading {{
  text-transform: uppercase;
  letter-spacing: 2px;
  color: #ff6ee6;
}}
.title-1, .title-2 {{
  font-family: {MONO};
  letter-spacing: 1px;
  text-shadow: 0 0 12px alpha({CYAN}, 0.55);
}}
button.suggested-action {{
  background-image: linear-gradient(90deg, #00d4ee, {MAGENTA});
  color: #05060c;
  box-shadow: 0 0 12px alpha({CYAN}, 0.35);
}}
button.suggested-action:hover {{
  box-shadow: 0 0 18px alpha({MAGENTA}, 0.55);
}}
button.destructive-action {{
  box-shadow: 0 0 12px alpha(#ff2b6d, 0.4);
}}
progressbar > trough > progress {{
  background-image: linear-gradient(90deg, {CYAN}, {MAGENTA});
  box-shadow: 0 0 10px alpha({CYAN}, 0.6);
}}
switch:checked {{
  box-shadow: 0 0 10px alpha({CYAN}, 0.45);
}}
image.success {{ -gtk-icon-shadow: 0 0 10px alpha({GREEN}, 0.8); }}
image.warning {{ -gtk-icon-shadow: 0 0 10px alpha({YELLOW}, 0.7); }}
image.error {{ -gtk-icon-shadow: 0 0 10px alpha({RED}, 0.8); }}
image.accent {{ -gtk-icon-shadow: 0 0 10px alpha({CYAN}, 0.8); }}
viewswitcher button:checked {{
  box-shadow: inset 0 -2px {CYAN};
}}
toast {{
  border: 1px solid alpha({MAGENTA}, 0.5);
}}
"""


def _colors_css() -> str:
    if (Adw.get_major_version(), Adw.get_minor_version()) >= (1, 6):
        lines = [f"  --{k.replace('_', '-')}: {v};" for k, v in PALETTE.items()]
        return ":root {\n" + "\n".join(lines) + "\n}\n"
    return "".join(f"@define-color {k} {v};\n" for k, v in PALETTE.items())


def settings_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "mikoshi" / "gui.json"


def neon_enabled() -> bool:
    try:
        return bool(json.loads(settings_path().read_text()).get("neon", True))
    except (OSError, ValueError):
        return True


def set_neon(on: bool) -> None:
    p = settings_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if p.exists():
            data = json.loads(p.read_text() or "{}")
        data["neon"] = bool(on)
        p.write_text(json.dumps(data, indent=2) + "\n")
    except (OSError, ValueError):
        pass
    apply()


_provider: Gtk.CssProvider | None = None


def apply() -> None:
    """Turn the neon theme on or off for the running app."""
    global _provider
    display = Gdk.Display.get_default()
    if display is None:
        return
    manager = Adw.StyleManager.get_default()
    if _provider is not None:
        Gtk.StyleContext.remove_provider_for_display(display, _provider)
        _provider = None
    if neon_enabled():
        manager.set_color_scheme(Adw.ColorScheme.FORCE_DARK)
        _provider = Gtk.CssProvider()
        _provider.load_from_string(_colors_css() + RULES)
        Gtk.StyleContext.add_provider_for_display(display, _provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
    else:
        manager.set_color_scheme(Adw.ColorScheme.DEFAULT)
