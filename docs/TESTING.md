# Testing Mikoshi on Arch

Test in a virtual machine first. Restores write to the system, and a VM can be thrown away.

## 1. Make a test VM

On Proxmox (or any hypervisor):

1. Create an Arch VM with a 30 GB system disk and a **second 20 GB disk** to act as the backup drive.
2. Install Arch with `archinstall`. Two layouts are worth covering:
   - **Simple:** ext4, no encryption, GRUB.
   - **Like a typical desktop:** LUKS + btrfs with subvolumes, systemd-boot, KDE or GNOME.
3. Format the second disk as ext4 and **don't** add it to fstab (Mikoshi mounts it itself):

   ```
   sudo mkfs.ext4 -L backup /dev/vdb
   ```

4. Optional: create an NFS share for testing on your NAS (a throwaway dataset).

## 2. Install from GitHub

```
sudo pacman -S --needed git base-devel
git clone https://github.com/NullWRX/Mikoshi
cd mikoshi/packaging/mikoshi-git
makepkg -si
```

To update later, run `makepkg -si` again in the same folder (it pulls the latest commit).
To remove everything: `sudo pacman -R mikoshi-gui-git mikoshi-git`.

## 3. Checklist

Tick these off in order. Anything odd: grab `sudo mikoshi log <job>` and
`journalctl -u 'mikoshi*'`.

**Basics**
- [ ] `mikoshi detect` shows the right firmware, filesystems, LUKS, bootloader and desktop.
- [ ] `mikoshi drives` lists the backup disk with its UUID.
- [ ] The app opens, and asks for your password once when you first need it.

**Destinations**
- [ ] Drive by UUID: `sudo mikoshi check <name>` says "Ready", and the drive isn't left mounted afterwards (`findmnt /run/mikoshi/mnt/*`).
- [ ] A FAT or exFAT USB stick is refused with a clear message.
- [ ] NFS: `check` reports the storage mode. `native` or `fake-super` is good. `lossy` means the export squashes root (on TrueNAS, set Maproot User to root).

**Backups**
- [ ] Run a job with the Essentials preset. Browse it in the app and on disk (`/run/mikoshi/...` or wherever the drive is).
- [ ] Run it again: "new data" should be tiny, and unchanged files share inodes (`ls -i` in two snapshots).
- [ ] `meta/packages/reinstall.sh` and `native.txt` look right.
- [ ] Cancel a large backup halfway (app or `sudo mikoshi cancel <job>`), then run it again: it continues and finishes.

**Schedules and triggers**
- [ ] `systemctl list-timers 'mikoshi*'` shows the job's next run.
- [ ] Set a schedule a few minutes ahead and let it fire. Then power off past a scheduled time and boot: the missed run happens.
- [ ] With "before pacman" on: `sudo pacman -S cowsay` runs a backup first; a second install within 10 minutes skips it.
- [ ] With "when the drive is plugged in" on: detach and re-attach the disk (Proxmox: Hardware → disk → Detach, then add it back; or a USB stick) and a backup starts.

**Restores**
- [ ] Change a dotfile, restore it in place: the preview shows it, the old version lands in `/var/lib/mikoshi/undo/`.
- [ ] Restore a folder to another folder: owners and permissions match the original.
- [ ] "Make folders match exactly" removes a file you created after the backup (preview shows it first).

**Retention**
- [ ] `sudo mikoshi prune --dry-run <job>` explains why each snapshot is kept.
- [ ] Pin a snapshot; it survives pruning.

**Failure cases**
- [ ] Unplug the drive mid-backup: the run fails cleanly, and the next run resumes.
- [ ] NAS down (stop its NFS service): the job fails within a minute or two and the PC doesn't freeze.
- [ ] Notifications: desktop pop-up on failure; ntfy if configured.

## 4. Moving to a real PC

Once the VM checklist passes, start with a small job (Essentials) to a second drive or a NAS
share, run it a few times, and try one restore to another folder before trusting it.
