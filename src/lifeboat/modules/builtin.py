"""The built-in backup modules."""

from __future__ import annotations

import fnmatch
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

from .. import system
from ..util import LifeboatError, run, run_out, which
from .base import Context, Module, Plan, existing, home_paths

# ---------------------------------------------------------------------------
# helpers for metadata tasks


def _write(path: Path, text: str, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, mode)


def _capture(cmd: list[str], out: Path, warnings: list[str], what: str, timeout: float = 120) -> bool:
    if not which(cmd[0]):
        return False
    try:
        proc = run(cmd, check=False, timeout=timeout)
    except LifeboatError as exc:
        warnings.append(f"{what}: {exc}")
        return False
    if proc.returncode != 0:
        warnings.append(f"{what}: {cmd[0]} exited {proc.returncode}: {(proc.stderr or '').strip()[:200]}")
        return False
    _write(out, proc.stdout)
    return True


# Electron and Chromium cache folders, found inside many app config folders.
CHROMIUM_CACHES = ["Cache", "Code Cache", "GPUCache", "CachedData", "ShaderCache", "GrShaderCache",
                   "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache", "Service Worker/CacheStorage",
                   "component_crx_cache", "Crashpad", "crashpad"]

CHROMIUM_BROWSERS = [".config/chromium", ".config/google-chrome", ".config/google-chrome-beta",
                     ".config/google-chrome-unstable", ".config/BraveSoftware", ".config/vivaldi",
                     ".config/vivaldi-snapshot", ".config/microsoft-edge", ".config/microsoft-edge-beta",
                     ".config/opera", ".config/thorium", ".config/ungoogled-chromium"]
FIREFOX_BROWSERS = [".mozilla", ".librewolf", ".floorp", ".zen", ".waterfox", ".thunderbird"]


def _cache_patterns(root: str) -> list[str]:
    return [f"{root}/**/{c}/" for c in CHROMIUM_CACHES]


# ---------------------------------------------------------------------------


class EtcModule(Module):
    id = "etc"
    name = "System settings (/etc)"
    summary = ("Everything in /etc: fstab, pacman.conf, network and service settings. "
               "Includes password hashes and SSH host keys.")

    def plan(self, ctx: Context) -> Plan:
        return Plan(paths=["/etc"], excludes=["/etc/pacman.d/gnupg/S.*", "/etc/.pwd.lock"])


DOTFILE_SKIP = {".Xauthority", ".ICEauthority", ".xsession-errors", ".xsession-errors.old",
                ".lesshst", ".wget-hsts", ".sudo_as_admin_successful", ".esd_auth", ".pulse-cookie",
                ".serverauth"}
DOT_DIRS = [".vim", ".emacs.d", ".doom.d", ".oh-my-zsh", ".zsh", ".zshrc.d", ".bashrc.d", ".tmux",
            ".local/bin", ".spacemacs.d", ".zprezto", ".config/zsh"]


class DotfilesModule(Module):
    id = "dotfiles"
    name = "Dotfiles"
    summary = "Hidden files in each home folder (.bashrc, .zshrc, .gitconfig...) and shell or editor folders like .vim."
    per_user = True

    def plan(self, ctx: Context) -> Plan:
        plan = Plan()
        for u in ctx.users:
            try:
                names = os.listdir(u.home)
            except OSError as exc:
                plan.warnings.append(f"Can't list {u.home}: {exc.strerror}")
                continue
            for n in names:
                p = os.path.join(u.home, n)
                if (n.startswith(".") and n not in DOTFILE_SKIP and not n.startswith(".xsession-errors")
                        and (os.path.isfile(p) or os.path.islink(p)) and not os.path.isdir(p)):
                    plan.paths.append(p)
            plan.paths += existing(home_paths(u, DOT_DIRS))
        return plan


class ConfigsModule(Module):
    id = "configs"
    name = "App settings"
    summary = "Each user's ~/.config folder and custom app launchers. Browser profiles have their own module."
    per_user = True

    def plan(self, ctx: Context) -> Plan:
        plan = Plan()
        for u in ctx.users:
            cfg = os.path.join(u.home, ".config")
            plan.paths += existing([cfg, os.path.join(u.home, ".local/share/applications")])
            plan.excludes += _cache_patterns(cfg)
            plan.soft_excludes += home_paths(u, [b for b in CHROMIUM_BROWSERS])
        return plan


DESKTOP_CONFIG_GLOBS = [
    # KDE Plasma
    "kdeglobals", "kwinrc", "kwinrulesrc", "plasma*", "kglobalshortcutsrc", "khotkeysrc",
    "ksmserverrc", "kscreenlockerrc", "krunnerrc", "kcminputrc", "kxkbrc", "kdedefaults",
    "kded5rc", "kded6rc", "powerdevilrc", "powermanagementprofilesrc", "breezerc", "klipperrc",
    "kaccessrc", "ktimezonedrc", "kwalletrc", "systemsettingsrc", "Trolltech.conf", "kactivitymanagerd*",
    "dolphinrc", "konsolerc", "katerc", "spectaclerc", "gtkrc", "gtkrc-2.0", "kde.org",
    # GNOME and GTK
    "gtk-3.0", "gtk-4.0", "gnome-*", "monitors.xml", "nautilus", "dconf", "gnome-session",
    # Xfce, Cinnamon, MATE, LXQt, Budgie, COSMIC, Deepin
    "xfce4", "Thunar", "cinnamon", "cinnamon-session", "nemo", "mate", "caja", "lxqt",
    "pcmanfm-qt", "openbox", "budgie-desktop", "cosmic", "deepin",
    # Window managers and their usual companions
    "hypr", "sway", "i3", "i3status", "i3blocks", "river", "niri", "wayfire.ini", "labwc", "qtile",
    "awesome", "bspwm", "sxhkd", "waybar", "polybar", "eww", "ags", "wofi", "rofi", "fuzzel",
    "tofi", "dunst", "mako", "swaync", "wlogout", "swaylock", "hyprlock", "hypridle", "picom",
    "kitty", "alacritty", "foot", "wezterm", "ghostty",
    # Look and feel shared by all desktops
    "qt5ct", "qt6ct", "Kvantum", "fontconfig", "environment.d", "autostart", "xsettingsd",
    "mimeapps.list", "user-dirs.dirs", "user-dirs.locale", "starship.toml", "fastfetch", "neofetch",
]
DESKTOP_SHARE = [".themes", ".icons", ".fonts", ".local/share/themes", ".local/share/icons",
                 ".local/share/fonts", ".local/share/backgrounds", ".local/share/wallpapers",
                 ".local/share/color-schemes", ".local/share/plasma", ".local/share/kxmlgui5",
                 ".local/share/konsole", ".local/share/kwin", ".local/share/aurorae",
                 ".local/share/plasma-systemmonitor", ".local/share/gnome-shell",
                 ".local/share/gnome-background-properties", ".local/share/nautilus",
                 ".local/share/applications", ".local/share/xfce4", ".local/share/cinnamon",
                 ".local/share/sddm", ".Xresources", ".Xdefaults", ".xinitrc", ".xprofile"]


class DesktopModule(Module):
    id = "desktop"
    name = "Desktop and theme"
    summary = ("Desktop settings for KDE, GNOME, Xfce, Cinnamon, Hyprland, Sway and others, plus "
               "themes, icons, fonts and wallpapers. GNOME settings are also saved as a text dump.")
    per_user = True

    def plan(self, ctx: Context) -> Plan:
        plan = Plan()
        for u in ctx.users:
            cfg = os.path.join(u.home, ".config")
            try:
                names = os.listdir(cfg)
            except OSError:
                names = []
            for n in names:
                if any(fnmatch.fnmatchcase(n, g) for g in DESKTOP_CONFIG_GLOBS):
                    plan.paths.append(os.path.join(cfg, n))
            plan.paths += existing(home_paths(u, DESKTOP_SHARE))
            plan.excludes += _cache_patterns(cfg)
            if os.path.exists(os.path.join(cfg, "dconf/user")) and which("dconf"):
                plan.meta.append((f"GNOME settings for {u.name}", _dconf_task(u)))
        return plan


def _dconf_task(u: system.User):
    def task(meta: Path) -> list[str]:
        warnings: list[str] = []
        env = {"HOME": u.home, "USER": u.name, "LOGNAME": u.name, "PATH": "/usr/bin:/bin"}
        runtime = f"/run/user/{u.uid}"
        if os.path.isdir(runtime):
            env["XDG_RUNTIME_DIR"] = runtime
        try:
            proc = run(["runuser", "-u", u.name, "--", "dconf", "dump", "/"], check=False, env=env, timeout=60)
        except LifeboatError as exc:
            return [f"dconf dump for {u.name}: {exc}"]
        if proc.returncode == 0 and proc.stdout.strip():
            _write(meta / f"dconf-{u.name}.ini", proc.stdout)
        elif proc.returncode != 0:
            warnings.append(f"dconf dump for {u.name} failed: {proc.stderr.strip()[:200]}")
        return warnings
    return task


REINSTALL_SH = """#!/usr/bin/env bash
# Reinstall the packages that were installed when this backup was made.
# Created by Lifeboat on {date} from {host}.
#   native.txt  = packages from the official repos (explicitly installed)
#   foreign.txt = AUR and other packages not in a repo
# Run as your normal user (it calls sudo for pacman). Review the lists first.
set -euo pipefail
cd "$(dirname "$0")"

available=$(mktemp)
trap 'rm -f "$available"' EXIT
pacman -Slq | sort -u > "$available"

missing=$(sort -u native.txt | comm -23 - "$available")
if [[ -n "$missing" ]]; then
    echo "Not in any enabled repo (skipped):"
    echo "$missing" | sed 's/^/  /'
fi
sort -u native.txt | comm -12 - "$available" | sudo pacman -S --needed -

if [[ -s foreign.txt ]]; then
    helper="${{AUR_HELPER:-{helper}}}"
    if command -v "$helper" >/dev/null 2>&1; then
        xargs -a foreign.txt "$helper" -S --needed
    else
        echo "AUR packages to install by hand (no AUR helper found):"
        sed 's/^/  /' foreign.txt
    fi
fi
"""


class PackagesModule(Module):
    id = "packages"
    name = "Package list"
    summary = ("Lists of installed packages (official repos and AUR) plus a script that reinstalls "
               "them on a fresh system. Tiny and quick.")

    def plan(self, ctx: Context) -> Plan:
        plan = Plan()
        if not which("pacman"):
            plan.warnings.append("pacman not found: package lists skipped")
            return plan
        plan.meta.append(("package lists", self._task(ctx)))
        return plan

    def _task(self, ctx: Context):
        def task(meta: Path) -> list[str]:
            w: list[str] = []
            _capture(["pacman", "-Qqen"], meta / "native.txt", w, "native package list")
            _capture(["pacman", "-Qqem"], meta / "foreign.txt", w, "foreign package list")
            _capture(["pacman", "-Q"], meta / "all-with-versions.txt", w, "full package list")
            # -Qqem exits 1 when there are no foreign packages; that's not a problem.
            w = [x for x in w if not x.startswith("foreign package list: pacman exited 1")]
            if not (meta / "foreign.txt").exists():
                _write(meta / "foreign.txt", "")
            _write(meta / "reinstall.sh", REINSTALL_SH.format(
                date=time.strftime("%Y-%m-%d %H:%M"), host=ctx.info.hostname,
                helper=ctx.info.aur_helper or "paru"), mode=0o755)
            return w
        return task


class DiskLayoutModule(Module):
    id = "disk-layout"
    name = "Boot and disk layout"
    summary = ("Partition tables, filesystem UUIDs, mounts, bootloader files (/boot), LVM and btrfs "
               "layout, and LUKS headers (only on encrypted destinations). Needed to rebuild a disk.")

    def plan(self, ctx: Context) -> Plan:
        plan = Plan()
        boot = ["/boot"]
        if ctx.info.esp and ctx.info.esp not in boot:
            boot.append(ctx.info.esp)
        plan.paths += existing(boot + ["/etc/fstab", "/etc/crypttab", "/etc/mkinitcpio.conf",
                                       "/etc/mkinitcpio.conf.d", "/etc/mkinitcpio.d", "/etc/default/grub",
                                       "/etc/kernel", "/etc/cmdline.d", "/etc/dracut.conf.d"])
        plan.meta.append(("disk layout", self._task(ctx)))
        if ctx.info.luks and not ctx.allow_secrets:
            plan.notes.append("LUKS header backup skipped: the destination isn't marked as encrypted.")
        return plan

    def _task(self, ctx: Context):
        def task(meta: Path) -> list[str]:
            w: list[str] = []
            _capture(["lsblk", "-J", "-O", "-b"], meta / "lsblk.json", w, "lsblk")
            _capture(["blkid"], meta / "blkid.txt", w, "blkid")
            _capture(["findmnt", "-J"], meta / "mounts.json", w, "findmnt")
            try:
                _write(meta / "cmdline.txt", Path("/proc/cmdline").read_text())
            except OSError:
                pass
            _write(meta / "system-info.json", json.dumps(ctx.info.to_dict(), indent=2))
            for d in system.lsblk():
                name = d.get("name") or ""
                if d.get("type") == "disk" and d.get("path") and not name.startswith(("zram", "loop", "ram")):
                    _capture(["sfdisk", "-d", d["path"]], meta / f"partitions-{name}.sfdisk", w,
                             f"partition table of {name}")
            if ctx.info.firmware == "uefi":
                _capture(["efibootmgr", "-v"], meta / "efibootmgr.txt", w, "UEFI boot entries")
            seen_btrfs = set()
            for m in system.system_mounts():
                if m.fstype == "btrfs":
                    dev = m.source.split("[")[0]
                    if dev in seen_btrfs:
                        continue
                    seen_btrfs.add(dev)
                    safe = dev.strip("/").replace("/", "_")
                    _capture(["btrfs", "subvolume", "list", "-t", m.target],
                             meta / f"btrfs-subvolumes-{safe}.txt", w, f"btrfs subvolumes on {dev}")
            if ctx.info.lvm and which("vgcfgbackup"):
                lvm_dir = meta / "lvm"
                lvm_dir.mkdir(parents=True, exist_ok=True)
                proc = run(["vgcfgbackup", "-f", str(lvm_dir / "%s.vg")], check=False, timeout=60)
                if proc.returncode != 0:
                    w.append(f"vgcfgbackup failed: {proc.stderr.strip()[:200]}")
            for dev in ctx.info.luks:
                safe = dev.strip("/").replace("/", "_")
                _capture(["cryptsetup", "luksDump", dev], meta / f"luks-{safe}.txt", w, f"LUKS info for {dev}")
                if ctx.allow_secrets:
                    out = meta / f"luks-header-{safe}.img"
                    proc = run(["cryptsetup", "luksHeaderBackup", dev, "--header-backup-file", str(out)],
                               check=False, timeout=60)
                    if proc.returncode != 0:
                        w.append(f"LUKS header backup of {dev} failed: {proc.stderr.strip()[:200]}")
                    else:
                        os.chmod(out, 0o600)
            return w
        return task


ENABLE_SH = """#!/usr/bin/env bash
# Re-enable the system services that were enabled when this backup was made.
# Created by Lifeboat on {date}. Review the list first; run with sudo.
set -uo pipefail
cd "$(dirname "$0")"
while read -r unit _; do
    [[ -z "$unit" ]] && continue
    systemctl enable "$unit" || echo "Could not enable $unit"
done < enabled-system.txt
"""


class ServicesModule(Module):
    id = "services"
    name = "Services and scheduled jobs"
    summary = "Which system services are enabled, systemd overrides, and crontabs, with a script to re-enable them."

    def plan(self, ctx: Context) -> Plan:
        plan = Plan(paths=existing(["/etc/systemd", "/var/spool/cron", "/etc/cron.d"]))
        for u in ctx.users:
            plan.paths += existing(home_paths(u, [".config/systemd"]))
        plan.meta.append(("enabled services", self._task()))
        return plan

    def _task(self):
        def task(meta: Path) -> list[str]:
            w: list[str] = []
            if not which("systemctl"):
                return w
            _capture(["systemctl", "list-unit-files", "--state=enabled", "--no-legend", "--no-pager",
                      "--type=service,timer,socket,path,mount,automount"],
                     meta / "enabled-system.txt", w, "enabled services")
            _capture(["systemctl", "get-default"], meta / "default-target.txt", w, "default target")
            _write(meta / "enable-services.sh", ENABLE_SH.format(date=time.strftime("%Y-%m-%d %H:%M")), 0o755)
            return w
        return task


HOME_SOFT_EXCLUDES = [".cache", ".local/share/Trash", ".thumbnails", ".local/share/baloo",
                      ".local/share/Steam/steamapps", ".steam/steam/steamapps",
                      ".local/share/containers/storage", ".local/share/flatpak/repo",
                      ".var/app/*/cache", ".npm/_cacache", ".cargo/registry", ".rustup/toolchains",
                      ".gradle/caches", ".m2/repository", ".local/share/gvfs-metadata", ".gvfs",
                      ".dbus", ".local/share/recently-used.xbel", ".ccache", ".cache.bak",
                      "go/pkg/mod", ".local/share/virtualenvs"]


class HomeModule(Module):
    id = "home"
    name = "Home folders"
    summary = ("Every file in each home folder, skipping caches, trash and big re-downloadable data "
               "(Steam games, package caches, toolchains).")
    per_user = True
    heavy = True

    def plan(self, ctx: Context) -> Plan:
        plan = Plan()
        for u in ctx.users:
            plan.paths.append(u.home)
            for rel in HOME_SOFT_EXCLUDES:
                p = os.path.join(u.home, rel)
                if "*" in rel:
                    plan.excludes.append(p)
                else:
                    plan.soft_excludes.append(p)
            plan.excludes += _cache_patterns(os.path.join(u.home, ".config"))
        return plan


SECRET_DIRS = [".ssh", ".gnupg", ".local/share/keyrings", ".password-store", ".pki", ".local/share/kwalletd"]


class SecretsModule(Module):
    id = "secrets"
    name = "Keys and passwords"
    summary = ("SSH keys, GPG keys, desktop keyrings and pass stores. Only saved to destinations "
               "marked as encrypted.")
    sensitive = True
    per_user = True

    def plan(self, ctx: Context) -> Plan:
        plan = Plan()
        for u in ctx.users:
            plan.paths += existing(home_paths(u, SECRET_DIRS))
            plan.excludes += [os.path.join(u.home, ".gnupg/S.*"), os.path.join(u.home, ".gnupg/*.lock")]
        plan.paths += existing(["/root/.ssh", "/root/.gnupg"])
        return plan


class BrowsersModule(Module):
    id = "browsers"
    name = "Browser profiles"
    summary = ("Bookmarks, history, extensions and logins for Firefox, Chromium-based browsers and "
               "their forks. Close browsers first for a clean copy.")
    per_user = True
    heavy = True

    def plan(self, ctx: Context) -> Plan:
        plan = Plan()
        for u in ctx.users:
            roots = existing(home_paths(u, CHROMIUM_BROWSERS + FIREFOX_BROWSERS))
            plan.paths += roots
            for r in roots:
                plan.excludes += _cache_patterns(r)
                plan.excludes += [f"{r}/**/cache2/", f"{r}/**/startupCache/", f"{r}/**/thumbnails/",
                                  f"{r}/**/minidumps/", f"{r}/**/Crash Reports/"]
        return plan


SYSTEM_EXCLUDES = [
    "/dev/*", "/proc/*", "/sys/*", "/run/*", "/tmp/*", "/mnt/*", "/media/*", "/lost+found",
    "/var/tmp/*", "/var/cache/pacman/pkg/*", "/var/lib/systemd/coredump/*", "/var/log/journal/*",
    "/swapfile", "/swap/*", "/.snapshots/*", "/home/.snapshots/*",
    "/var/lib/docker/overlay2/*", "/var/lib/containers/storage/overlay/*",
    "/home/*/.cache/*", "/home/*/.local/share/Trash/*", "/root/.cache/*",
    "/home/*/.gvfs", "/var/lib/lifeboat/undo/*",
]


class SystemModule(Module):
    id = "system"
    name = "Full system"
    summary = ("Every file on the system's own disks (/, /home, /boot and other local partitions) for "
               "a full rebuild. Skips caches, temporary files and other drives.")
    heavy = True

    def plan(self, ctx: Context) -> Plan:
        plan = Plan()
        plan.paths = [m.target for m in system.system_mounts()] or ["/"]
        plan.excludes = list(SYSTEM_EXCLUDES)
        plan.notes.append("Full system copies are taken while the system runs; databases and VMs "
                          "that are in use may be copied mid-write.")
        return plan
