"""Detect how this Arch install is put together.

Nothing here assumes a particular layout: every probe tolerates missing
tools and odd setups, and falls back to "unknown" instead of failing.
"""

from __future__ import annotations

import json
import os
import pwd
import socket
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path

from .util import run_out, which

# Filesystems that hold real local data (as opposed to proc, tmpfs, network...).
LOCAL_FS = {"ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "vfat", "exfat", "ntfs", "ntfs3",
            "jfs", "reiserfs", "zfs", "bcachefs", "nilfs2"}
NETWORK_FS = {"nfs", "nfs4", "cifs", "smb3", "sshfs", "fuse.sshfs", "9p", "ceph", "glusterfs"}

# Mount points that are never part of "the system".
NON_SYSTEM_MOUNTS = ("/proc", "/sys", "/dev", "/run", "/tmp", "/mnt", "/media", "/var/run",
                     "/var/lib/docker", "/var/lib/containers", "/.snapshots", "/home/.snapshots")


@dataclass
class User:
    name: str
    uid: int
    gid: int
    home: str


@dataclass
class Mount:
    target: str
    source: str
    fstype: str
    options: str


@dataclass
class SystemInfo:
    hostname: str = ""
    firmware: str = "unknown"            # uefi | bios
    root_fs: str = "unknown"
    home_fs: str = "unknown"
    luks: list[str] = field(default_factory=list)      # LUKS device paths
    lvm: bool = False
    btrfs: bool = False
    bootloaders: list[str] = field(default_factory=list)
    esp: str = ""                        # mount point of the EFI system partition
    desktops: list[str] = field(default_factory=list)
    aur_helper: str = ""
    users: list[str] = field(default_factory=list)
    kernel: str = ""
    pacman: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# users

def login_defs_uid_range() -> tuple[int, int]:
    lo, hi = 1000, 60000
    try:
        for line in Path("/etc/login.defs").read_text().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] == "UID_MIN":
                lo = int(parts[1])
            elif len(parts) == 2 and parts[0] == "UID_MAX":
                hi = int(parts[1])
    except (OSError, ValueError):
        pass
    return lo, hi


def regular_users() -> list[User]:
    """People who log in: UID in the login range, a real shell and an existing home."""
    lo, hi = login_defs_uid_range()
    out = []
    for pw in pwd.getpwall():
        if not lo <= pw.pw_uid <= hi:
            continue
        if pw.pw_shell.endswith(("nologin", "/false")):
            continue
        if not pw.pw_dir or pw.pw_dir == "/" or not os.path.isdir(pw.pw_dir):
            continue
        out.append(User(pw.pw_name, pw.pw_uid, pw.pw_gid, os.path.normpath(pw.pw_dir)))
    return sorted(out, key=lambda u: u.uid)


def users_for(names: list[str]) -> list[User]:
    """Users selected by a job. Empty list means everyone."""
    everyone = regular_users()
    if not names:
        return everyone
    out = []
    for n in names:
        try:
            pw = pwd.getpwnam(n)
        except KeyError:
            continue
        if os.path.isdir(pw.pw_dir):
            out.append(User(pw.pw_name, pw.pw_uid, pw.pw_gid, os.path.normpath(pw.pw_dir)))
    return out


# ---------------------------------------------------------------------------
# mounts and block devices

def _unescape(s: str) -> str:
    # /proc/self/mountinfo escapes space, tab, newline and backslash as \ooo.
    out, i = [], 0
    while i < len(s):
        if s[i] == "\\" and i + 4 <= len(s) and s[i + 1:i + 4].isdigit():
            out.append(chr(int(s[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


def mounts() -> list[Mount]:
    out = []
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        left, _, right = line.partition(" - ")
        lp = left.split()
        rp = right.split()
        if len(lp) < 6 or len(rp) < 2:
            continue
        out.append(Mount(target=_unescape(lp[4]), source=_unescape(rp[1]), fstype=rp[0],
                         options=lp[5] + ("," + rp[2] if len(rp) > 2 else "")))
    return out


def mount_for(path: str) -> Mount | None:
    """The mount that contains path (longest matching mount point)."""
    path = os.path.realpath(path)
    best = None
    for m in mounts():
        t = m.target
        if path == t or path.startswith(t.rstrip("/") + "/") or t == "/":
            if best is None or len(t) >= len(best.target):
                best = m
    return best


def fstype_of(path: str) -> str:
    m = mount_for(path)
    return m.fstype if m else "unknown"


def system_mounts() -> list[Mount]:
    """Local filesystems that make up the installed system (/, /home, /boot, /var/log...)."""
    out = []
    for m in mounts():
        if m.fstype not in LOCAL_FS:
            continue
        if m.target != "/" and any(m.target == p or m.target.startswith(p + "/") for p in NON_SYSTEM_MOUNTS):
            continue
        if "/.snapshots" in m.target:
            continue
        out.append(m)
    # A path can be mounted more than once (bind mounts); keep the first.
    seen, uniq = set(), []
    for m in out:
        if m.target not in seen:
            seen.add(m.target)
            uniq.append(m)
    return uniq


@lru_cache(maxsize=1)
def lsblk() -> list[dict]:
    """Flat list of block devices with the fields Mikoshi needs."""
    cols = "NAME,PATH,TYPE,FSTYPE,UUID,LABEL,SIZE,MODEL,TRAN,RM,HOTPLUG,PKNAME,MOUNTPOINTS"
    raw = run_out(["lsblk", "-J", "-b", "-o", cols])
    if not raw:
        return []
    try:
        tree = json.loads(raw).get("blockdevices", [])
    except json.JSONDecodeError:
        return []
    flat: list[dict] = []

    def walk(nodes, parent=None):
        for n in nodes:
            n = dict(n)
            children = n.pop("children", [])
            n.setdefault("pkname", parent)
            n["mountpoints"] = [m for m in (n.get("mountpoints") or []) if m]
            flat.append(n)
            walk(children, n.get("name"))

    walk(tree)
    return flat


def disks_for_device(source: str) -> set[str]:
    """The physical disks under a device (follows LUKS, LVM and partitions)."""
    if not source.startswith("/dev/"):
        return set()
    out = run_out(["lsblk", "-s", "-n", "-r", "-o", "NAME,TYPE", source])
    return {line.split()[0] for line in out.splitlines() if line.split()[1:] == ["disk"]}


def disks_for_path(path: str) -> set[str]:
    m = mount_for(path)
    if not m:
        return set()
    src = m.source
    if m.fstype == "btrfs" and "[" in src:
        src = src.split("[")[0]
    return disks_for_device(src)


# ---------------------------------------------------------------------------
# boot

def firmware() -> str:
    return "uefi" if os.path.isdir("/sys/firmware/efi") else "bios"


def esp_mount() -> str:
    for m in mounts():
        if m.fstype == "vfat" and m.target in ("/boot", "/efi", "/boot/efi"):
            return m.target
    return ""


def bootloaders() -> list[str]:
    found = []
    esp = esp_mount()
    roots = [p for p in {esp, "/boot", "/efi", "/boot/efi"} if p and os.path.isdir(p)]

    def exists_any(*rel):
        return any(os.path.exists(os.path.join(r, x)) for r in roots for x in rel)

    if exists_any("loader/loader.conf", "EFI/systemd/systemd-bootx64.efi"):
        found.append("systemd-boot")
    if os.path.exists("/boot/grub/grub.cfg") or exists_any("EFI/grub", "EFI/GRUB"):
        found.append("grub")
    if exists_any("EFI/refind/refind.conf", "EFI/refind") or os.path.exists("/boot/refind_linux.conf"):
        found.append("refind")
    if exists_any("limine.conf", "limine.cfg", "limine/limine.conf", "EFI/limine", "EFI/BOOT/limine.conf"):
        found.append("limine")
    if exists_any("EFI/Linux"):
        try:
            if any(f.endswith(".efi") for r in roots for f in os.listdir(os.path.join(r, "EFI/Linux"))
                   if os.path.isdir(os.path.join(r, "EFI/Linux"))):
                found.append("uki")
        except OSError:
            pass
    return found


# ---------------------------------------------------------------------------
# desktop environments

_SESSIONS = {
    "plasma": "kde", "plasmawayland": "kde", "plasmax11": "kde",
    "gnome": "gnome", "gnome-xorg": "gnome", "gnome-wayland": "gnome",
    "xfce": "xfce", "cinnamon": "cinnamon", "cinnamon-wayland": "cinnamon",
    "mate": "mate", "budgie-desktop": "budgie", "lxqt": "lxqt", "lxde": "lxde",
    "hyprland": "hyprland", "hyprland-uwsm": "hyprland", "sway": "sway", "i3": "i3",
    "river": "river", "niri": "niri", "wayfire": "wayfire", "qtile": "qtile",
    "awesome": "awesome", "bspwm": "bspwm", "openbox": "openbox", "cosmic": "cosmic",
    "deepin": "deepin", "enlightenment": "enlightenment", "labwc": "labwc",
}


def desktops() -> list[str]:
    found = set()
    for d in ("/usr/share/xsessions", "/usr/share/wayland-sessions"):
        try:
            for f in os.listdir(d):
                if f.endswith(".desktop"):
                    key = f[:-8].lower()
                    if key in _SESSIONS:
                        found.add(_SESSIONS[key])
        except OSError:
            pass
    return sorted(found)


def aur_helper() -> str:
    for h in ("paru", "yay", "pikaur", "aura", "trizen", "pakku"):
        if which(h):
            return h
    return ""


def luks_devices() -> list[str]:
    return [d["path"] for d in lsblk() if d.get("fstype") == "crypto_LUKS" and d.get("path")]


def has_lvm() -> bool:
    return any(d.get("type") == "lvm" for d in lsblk())


def hostname() -> str:
    name = socket.gethostname().split(".")[0].lower()
    return "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:32] or "localhost"


def detect() -> SystemInfo:
    root = mount_for("/")
    home = mount_for("/home")
    info = SystemInfo(
        hostname=hostname(),
        firmware=firmware(),
        root_fs=root.fstype if root else "unknown",
        home_fs=home.fstype if home else "unknown",
        luks=luks_devices(),
        lvm=has_lvm(),
        btrfs=any(m.fstype == "btrfs" for m in system_mounts()),
        bootloaders=bootloaders(),
        esp=esp_mount(),
        desktops=desktops(),
        aur_helper=aur_helper(),
        users=[u.name for u in regular_users()],
        kernel=os.uname().release,
        pacman=bool(which("pacman")),
    )
    return info
