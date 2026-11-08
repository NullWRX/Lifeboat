"""Backup destinations: open (mount) them, and test what they can store.

Three kinds:
  folder  any existing folder, normally on a second drive that's always mounted
  drive   a drive found by filesystem UUID; mounted (and unlocked) on demand
  nfs     an NFS export; Mikoshi mounts it itself with soft options so a dead
          NAS makes the job fail instead of hanging the machine
"""

from __future__ import annotations

import contextlib
import os
import shutil
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterator

from . import paths, system
from .config import Destination
from .util import MikoshiError, Unavailable, log, run, run_out, which

# Filesystems that keep Linux ownership, permissions, ACLs and xattrs natively.
NATIVE_FS = {"ext2", "ext3", "ext4", "btrfs", "xfs", "f2fs", "bcachefs", "jfs", "reiserfs", "nilfs2"}
NO_HARDLINK_FS = {"vfat", "msdos", "exfat", "fuseblk"}
NFS_PORT = 2049


class Locked(Unavailable):
    """An encrypted (LUKS) drive that hasn't been unlocked."""

    def __init__(self, uuid: str):
        self.uuid = uuid
        super().__init__("The backup drive is encrypted and locked. Unlock it, or set a key file.")


@dataclass
class Capabilities:
    fstype: str = "unknown"
    hardlinks: bool = False
    symlinks: bool = False
    chown: bool = False
    xattr: bool = False
    acl: bool = False
    free_bytes: int = 0
    total_bytes: int = 0
    same_disk: bool = False
    encrypted: bool = False        # detected: sits on LUKS/dm-crypt
    mode: str = "native"           # native | fake-super | basic | lossy
    problems: list[str] = field(default_factory=list)   # blocking
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    def rsync_flags(self) -> list[str]:
        """rsync options that store as much as this destination can hold."""
        if self.mode == "native":
            flags = ["-aH", "--numeric-ids"]
            if self.acl:
                flags.append("-A")
            if self.xattr:
                flags.append("-X")
            return flags
        if self.mode == "fake-super":
            # Ownership, permissions, devices, ACLs and privileged xattrs are kept inside
            # user.rsync.* xattrs on the destination. The receiving side runs with
            # --fake-super through engine.fake_super_opts().
            return ["-aHAX", "--numeric-ids"]
        if self.mode == "basic":
            return ["-aH", "--numeric-ids"]
        return ["-rlptH", "--no-owner", "--no-group"]


@dataclass
class Opened:
    dest: Destination
    mount_point: str
    base: Path            # the folder the user chose (mount point + subdir)
    repo: Path            # base/mikoshi/<hostname>
    encrypted: bool       # detected or declared
    mounted_by_us: bool = False


# ---------------------------------------------------------------------------
# helpers

def _mount_dir(name: str) -> Path:
    d = paths.MOUNT_DIR / name
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def _umount(target: str) -> None:
    # Never unmount lazily: another Mikoshi process (a backup started while the GUI was
    # browsing) may be writing through this mount. If it's busy, leave it; the next run reuses it.
    try:
        proc = run(["umount", target], check=False, timeout=120)
    except MikoshiError as exc:
        log.warning("umount %s: %s", target, exc)
        return
    if proc.returncode != 0:
        log.info("left %s mounted (%s)", target, proc.stderr.strip())
        return
    with contextlib.suppress(OSError):
        os.rmdir(target)


def _is_rw(mount_point: str) -> bool:
    m = system.mount_for(mount_point)
    # The first per-mount option in mountinfo is always rw or ro.
    return bool(m) and m.options.split(",")[0] == "rw"


def _on_crypt(path_or_dev: str) -> bool:
    dev = path_or_dev
    if not dev.startswith("/dev/"):
        m = system.mount_for(path_or_dev)
        if not m:
            return False
        dev = m.source.split("[")[0]
    if not dev.startswith("/dev/"):
        return False
    out = run_out(["lsblk", "-s", "-n", "-r", "-o", "TYPE", dev])
    return "crypt" in out.split()


def nfs_reachable(server: str, timeout: float = 5.0) -> bool:
    try:
        with socket.create_connection((server.strip("[]"), NFS_PORT), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# open

@contextlib.contextmanager
def open_destination(dest: Destination, hostname: str, *, create: bool = True) -> Iterator[Opened]:
    """Make a destination usable for the length of the block, then put it back as it was."""
    cleanups: list = []
    try:
        if dest.type == "folder":
            opened = _open_folder(dest, hostname)
        elif dest.type == "drive":
            opened = _open_drive(dest, hostname, cleanups)
        elif dest.type == "nfs":
            opened = _open_nfs(dest, hostname, cleanups)
        else:
            raise MikoshiError(f"Unknown destination type {dest.type}")
        migrate_repo_dir(opened.base)
        if create:
            _make_repo(opened)
        yield opened
    finally:
        for fn in reversed(cleanups):
            try:
                fn()
            except Exception as exc:  # cleanup must never mask the real error
                log.warning("cleanup failed: %s", exc)


def permission_message(path) -> str:
    """Explain a 'permission denied' (error 13) on a destination in terms of what to fix."""
    fs = system.fstype_of(str(path))
    if fs.startswith("nfs"):
        return (f"The NAS won't let this PC's root user write to {path}. NFS shares usually turn root "
                "into a nobody user (\"root squash\"), and Mikoshi runs as root. Fix it on the NAS: on "
                "TrueNAS, edit the NFS share, open Advanced Options and set Maproot User and Maproot Group "
                "to root (limit the share to this PC's IP address). On other NAS systems, allow "
                "no_root_squash for this PC.")
    if fs in ("cifs", "smb3"):
        return (f"{path} is an SMB/Windows share, which can't keep Linux file owners and doesn't let root "
                "write by default. Use an NFS share or a local drive instead.")
    return (f"Mikoshi (running as root) isn't allowed to write to {path}. Check that the folder isn't "
            "on a read-only or restricted mount, and pick a different folder if needed.")


def _mkdir(path: Path, **kw) -> None:
    try:
        path.mkdir(**kw)
    except PermissionError:
        raise MikoshiError(permission_message(path.parent if not path.exists() else path)) from None


LEGACY_REPO_DIRNAME = "lifeboat"   # this project's earlier name


def migrate_repo_dir(base: Path) -> None:
    """Rename <base>/lifeboat to <base>/mikoshi (backups made before the rename)."""
    old, new = base / LEGACY_REPO_DIRNAME, base / paths.REPO_DIRNAME
    try:
        if old.is_dir() and not new.exists():
            os.rename(old, new)
            readme = new / "README.txt"
            if readme.exists():
                readme.write_text(readme.read_text().replace("Lifeboat", "Mikoshi"))
            log.info("renamed %s to %s", old, new)
    except OSError as exc:
        log.warning("couldn't rename %s: %s", old, exc)


def _make_repo(opened: Opened) -> None:
    if not opened.base.is_dir():
        raise MikoshiError(f"{opened.base} doesn't exist or isn't a folder")
    top = opened.base / paths.REPO_DIRNAME
    try:
        if not top.exists():
            top.mkdir(mode=0o700)
            (top / "README.txt").write_text(
                "Backups made by Mikoshi (rsync snapshots).\n"
                "Each snapshot folder holds files/ (a normal copy of the backed-up paths),\n"
                "meta/ (package lists, disk layout...) and manifest.json.\n"
                "Unchanged files are hard links between snapshots, so every snapshot is complete\n"
                "but only changed files take extra space. Restore with Mikoshi or plain cp/rsync.\n")
        opened.repo.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise MikoshiError(permission_message(opened.base)) from None


def _open_folder(dest: Destination, hostname: str) -> Opened:
    base = Path(dest.path)
    if not base.is_dir():
        raise Unavailable(f"Folder {dest.path} doesn't exist. Is the drive mounted?")
    m = system.mount_for(str(base))
    if not dest.same_disk_ok:
        root_disks = system.disks_for_path("/")
        here = system.disks_for_path(str(base))
        if root_disks and here and root_disks & here:
            raise MikoshiError(
                f"{dest.path} is on the same disk as the system, so a disk failure would take the "
                "backups with it. If the drive isn't mounted, mount it. To allow this anyway, set "
                "same_disk_ok = true on the destination.")
    if not _is_rw(str(base)):
        raise MikoshiError(f"{dest.path} is mounted read-only")
    return Opened(dest, m.target if m else "/", base, base / paths.REPO_DIRNAME / hostname,
                  encrypted=dest.encrypted or _on_crypt(str(base)))


def _find_dev(uuid: str) -> dict | None:
    system.lsblk.cache_clear()
    for d in system.lsblk():
        if (d.get("uuid") or "").lower() == uuid.lower():
            return d
    return None


def _open_drive(dest: Destination, hostname: str, cleanups: list) -> Opened:
    dev = _find_dev(dest.uuid)
    if not dev or not os.path.exists(f"/dev/disk/by-uuid/{dest.uuid}"):
        raise Unavailable("The backup drive isn't connected.")
    encrypted = dest.encrypted
    fsdev = dev
    if dev.get("fstype") == "crypto_LUKS":
        encrypted = True
        child = next((d for d in system.lsblk() if d.get("pkname") == dev.get("name") and d.get("type") == "crypt"), None)
        if child is None:
            if not dest.keyfile:
                raise Locked(dest.uuid)
            if not os.path.isfile(dest.keyfile):
                raise MikoshiError(f"Key file {dest.keyfile} doesn't exist")
            mapper = f"mikoshi-{dest.uuid[:8].lower()}"
            run(["cryptsetup", "open", "--key-file", dest.keyfile, dev["path"], mapper], timeout=120)
            cleanups.append(lambda: run(["cryptsetup", "close", mapper], check=False, timeout=60))
            system.lsblk.cache_clear()
            child = next((d for d in system.lsblk() if d.get("name") == mapper), None)
            if child is None:
                raise MikoshiError("Unlocked the drive but can't find its filesystem")
        fsdev = child
    else:
        encrypted = encrypted or _on_crypt(dev["path"])

    if fsdev.get("fstype") in NO_HARDLINK_FS:
        raise MikoshiError(f"The drive uses {fsdev.get('fstype')}, which can't store Linux snapshots. "
                            "Reformat it as ext4, btrfs or xfs.")
    mps = [m for m in fsdev.get("mountpoints") or [] if m and m != "[SWAP]"]
    rw = [m for m in mps if _is_rw(m)]
    if rw:
        mount_point, ours = rw[0], False
    else:
        target = _mount_dir(f"drive-{dest.uuid.lower()}")
        if os.path.ismount(target):
            _umount(str(target))
            target.mkdir(parents=True, exist_ok=True)
        run(["mount", "-o", "noatime", fsdev["path"], str(target)], timeout=120)
        cleanups.append(lambda t=str(target): _umount(t))
        mount_point, ours = str(target), True
    base = Path(mount_point) / dest.subdir if dest.subdir else Path(mount_point)
    _mkdir(base, parents=True, exist_ok=True)
    return Opened(dest, mount_point, base, base / paths.REPO_DIRNAME / hostname, encrypted, ours)


def _open_nfs(dest: Destination, hostname: str, cleanups: list) -> Opened:
    if not nfs_reachable(dest.server):
        raise Unavailable(f"The NFS server {dest.server} isn't reachable.")
    target = _mount_dir(f"nfs-{dest.name or 'share'}")
    source = f"{dest.server}:{dest.export}"
    ours = False
    if os.path.ismount(target):
        current = system.mount_for(str(target))
        if not current or current.source.rstrip("/") != source.rstrip("/"):
            _umount(str(target))
            if os.path.ismount(target):
                raise MikoshiError(f"{target} is busy with a different share")
    if not os.path.ismount(target):
        target.mkdir(parents=True, exist_ok=True)
        # soft + short timeouts: if the NAS dies mid-run the job fails instead of freezing.
        opts = "soft,timeo=150,retrans=3,noatime" + (f",{dest.options}" if dest.options else "")
        proc = run(["mount", "-t", "nfs", "-o", opts, source, str(target)], check=False, timeout=90)
        if proc.returncode != 0:
            raise MikoshiError(f"Couldn't mount {source}: {proc.stderr.strip() or proc.stdout.strip()}")
        cleanups.append(lambda t=str(target): _umount(t))
        ours = True
    base = target / dest.subdir if dest.subdir else target
    _mkdir(base, parents=True, exist_ok=True)
    return Opened(dest, str(target), base, base / paths.REPO_DIRNAME / hostname, dest.encrypted, ours)


# ---------------------------------------------------------------------------
# probe

def probe(opened: Opened) -> Capabilities:
    """Test what the destination can store, by trying it."""
    cap = Capabilities()
    cap.fstype = system.fstype_of(str(opened.repo))
    cap.encrypted = opened.encrypted
    try:
        st = os.statvfs(opened.repo)
        cap.free_bytes = st.f_bavail * st.f_frsize
        cap.total_bytes = st.f_blocks * st.f_frsize
    except OSError:
        pass
    d = opened.repo / f".probe-{os.getpid()}"
    shutil.rmtree(d, ignore_errors=True)
    try:
        d.mkdir()
        f = d / "file"
        f.write_text("mikoshi probe\n")
        try:
            os.link(f, d / "link")
            cap.hardlinks = os.stat(f).st_nlink == 2
        except OSError:
            cap.hardlinks = False
        try:
            os.symlink("file", d / "sym")
            cap.symlinks = os.path.islink(d / "sym")
        except OSError:
            cap.symlinks = False
        try:
            os.chown(f, 54321, 54321)
            s = os.stat(f)
            cap.chown = s.st_uid == 54321 and s.st_gid == 54321
        except OSError:
            cap.chown = False
        try:
            os.setxattr(f, "user.mikoshi", b"1")
            cap.xattr = os.getxattr(f, "user.mikoshi") == b"1"
        except OSError:
            cap.xattr = False
        if which("setfacl"):
            cap.acl = run(["setfacl", "-m", "u:54321:r", str(f)], check=False, timeout=30).returncode == 0
        else:
            cap.acl = cap.fstype in NATIVE_FS
    except PermissionError:
        cap.problems.append(permission_message(opened.repo))
        return cap
    except OSError as exc:
        cap.problems.append(f"Can't write to {opened.repo}: {exc.strerror}")
        return cap
    finally:
        shutil.rmtree(d, ignore_errors=True)

    if cap.fstype in NATIVE_FS or (cap.fstype == "zfs" and cap.chown):
        cap.mode = "native"
        if not cap.xattr:
            cap.warnings.append("Extended attributes (file capabilities, SELinux labels) won't be kept.")
        if not cap.acl:
            cap.warnings.append("ACLs won't be kept.")
    elif cap.xattr:
        cap.mode = "fake-super"
        cap.warnings.append("Owners and permissions are stored in extended attributes, because this "
                            "destination can't store them directly. Restore with Mikoshi (or rsync --fake-super).")
    elif cap.chown:
        cap.mode = "basic"
        cap.warnings.append("ACLs and extended attributes (file capabilities) won't be kept.")
    else:
        cap.mode = "lossy"
        msg = "File owners and permissions can't be kept on this destination."
        if cap.fstype.startswith("nfs"):
            msg += " The NFS export maps root to nobody (root squash); allow root on the export (TrueNAS: Maproot User = root)."
        cap.warnings.append(msg)

    if opened.dest.type == "folder" and cap.fstype.startswith("nfs"):
        cap.warnings.append("This folder is on an NFS share mounted by the system. If the NAS goes down, "
                            "a backup can freeze waiting for it (hard mounts do that). Use the Network "
                            "share (NFS) destination type instead: Mikoshi mounts it with timeouts.")
    if not cap.hardlinks:
        cap.problems.append(f"This destination ({cap.fstype}) can't store hard links, which snapshots need. "
                            "Use an ext4, btrfs or xfs drive, or an NFS share.")
    if not cap.symlinks:
        cap.problems.append("This destination can't store symbolic links.")
    root_disks = system.disks_for_path("/")
    here = system.disks_for_path(str(opened.repo))
    cap.same_disk = bool(root_disks and here and root_disks & here)
    if cap.same_disk:
        cap.warnings.append("This destination is on the same disk as the system. It protects against "
                            "mistakes, not against a disk failure.")
    return cap


# ---------------------------------------------------------------------------
# listing

def list_drives() -> list[dict]:
    """Filesystems the user could pick as a backup drive."""
    system.lsblk.cache_clear()
    devs = system.lsblk()
    by_name = {d.get("name"): d for d in devs}
    sys_targets = {m.target for m in system.system_mounts()}
    out = []
    for d in devs:
        fstype = d.get("fstype")
        if not fstype or not d.get("uuid") or fstype in ("swap", "LVM2_member", "linux_raid_member", "iso9660", "squashfs"):
            continue
        if d.get("type") in ("loop", "rom"):
            continue
        # A crypt mapping is shown through its LUKS parent instead.
        if d.get("type") == "crypt":
            continue
        disk = d
        for _ in range(6):
            if disk.get("type") == "disk" or disk.get("pkname") not in by_name:
                break
            disk = by_name[disk["pkname"]]
        child_mps: list[str] = []
        if fstype == "crypto_LUKS":
            for c in devs:
                if c.get("pkname") == d.get("name"):
                    child_mps += c.get("mountpoints") or []
        mps = (d.get("mountpoints") or []) + child_mps
        out.append({
            "uuid": d.get("uuid"),
            "path": d.get("path"),
            "label": d.get("label") or "",
            "fstype": fstype,
            "size": int(d.get("size") or 0),
            "model": (disk.get("model") or "").strip(),
            "transport": disk.get("tran") or "",
            "removable": bool(disk.get("rm") or disk.get("hotplug")),
            "mountpoints": mps,
            "encrypted": fstype == "crypto_LUKS",
            "system": any(m in sys_targets for m in mps),
            "usable": fstype not in NO_HARDLINK_FS,
        })
    return out


def unlock_drive(uuid: str, passphrase: str) -> str:
    """Unlock a LUKS drive with its passphrase (stays unlocked until locked or unplugged)."""
    dev = _find_dev(uuid)
    if not dev or dev.get("fstype") != "crypto_LUKS":
        raise MikoshiError("That drive isn't connected, or isn't encrypted.")
    if any(d.get("pkname") == dev.get("name") and d.get("type") == "crypt" for d in system.lsblk()):
        return "already unlocked"
    mapper = f"mikoshi-{uuid[:8].lower()}"
    proc = run(["cryptsetup", "open", "--key-file=-", dev["path"], mapper], check=False,
               input=passphrase, timeout=120)
    if proc.returncode == 2:
        raise MikoshiError("Wrong passphrase.")
    if proc.returncode != 0:
        raise MikoshiError(f"Couldn't unlock the drive: {proc.stderr.strip()}")
    system.lsblk.cache_clear()
    return "unlocked"


def nfs_exports(server: str) -> list[str]:
    if not which("showmount"):
        raise MikoshiError("showmount isn't installed (package nfs-utils)")
    out = run(["showmount", "-e", "--no-headers", server], timeout=20).stdout
    return [line.split()[0] for line in out.splitlines() if line.strip()]
