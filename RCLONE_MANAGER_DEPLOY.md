# RCLONE MANAGER Deploy Guide

`RCLONE MANAGER` ships as one codebase with two install modes:

- `linux`: standard Linux distributions with `systemd`
- `unraid`: Unraid hosts using persistent files and boot-time scripts

The installer auto-detects the platform, so most users only need:

```bash
git clone <repo-url>
cd rclone-manager
sudo bash install.sh
```

## Linux vs Unraid

| Area | Linux mode | Unraid mode |
| --- | --- | --- |
| Startup model | `systemd` service | `/boot/config/go` boot hook |
| App install path | `/opt/rclone-mount-manager` | `/boot/config/plugins/rclone-mount-manager/app` |
| App env file | `/etc/default/rclone-mount-manager` | `/boot/config/plugins/rclone-mount-manager/manager.env` |
| Mount persistence | `rclone-*.service` units | JSON mount definitions + boot restore |
| Mount control | `systemctl` | direct `rclone mount` processes |
| Config persistence | distro filesystem | `/boot/config/...` persistent flash storage |
| Cache handling | whatever you set per mount | auto-generated per-mount cache dirs under `/mnt/cache/appdata/rclone-manager` |

## Linux install

Requirements:

- `python3`
- `rclone`
- `systemd`
- FUSE support for `rclone mount`

Install:

```bash
git clone <repo-url>
cd rclone-manager
sudo RCLONE_MANAGER_USER=admin RCLONE_MANAGER_PASS='change-me' bash install.sh
```

Then open:

```text
http://<server-ip>:5573
```

## Unraid install

Requirements:

- Unraid host shell access
- `python3`
- `rclone`
- FUSE support for `rclone mount`
- `rclone.conf` available on the host

Install:

```bash
git clone <repo-url>
cd rclone-manager
sudo RCLONE_MANAGER_USER=admin RCLONE_MANAGER_PASS='change-me' bash install.sh
```

What the Unraid installer does:

- copies the app into `/boot/config/plugins/rclone-mount-manager`
- creates persistent mount, pid, and log folders there
- creates the default Unraid cache base at `/mnt/cache/appdata/rclone-manager` when available
- writes `/boot/config/plugins/rclone-mount-manager/manager.env`
- adds a managed startup block to `/boot/config/go`
- starts the web UI
- waits for `/mnt/user` and then restores saved mounts

Why the Unraid defaults were tightened:

- many Unraid mounts fail unless `rclone` is pointed at the host's real `rclone.conf`
- media-oriented mounts are more stable when they use a persistent VFS cache path instead of an implicit temp directory
- Unraid shares commonly need `uid 99`, `gid 100`, and `umask 002` so Plex-style apps can read them cleanly
- several users were pasting working shell scripts by hand, so the manager now generates that same pattern automatically for new UI-created mounts

New UI-created Unraid mounts now get these defaults automatically unless you override them:

- `--config /boot/config/plugins/rclone/.rclone.conf` (or the detected override)
- `--cache-dir /mnt/cache/appdata/rclone-manager/<service>-vfs-cache`
- `--uid 99 --gid 100 --umask 002`
- `--vfs-cache-mode full`
- `--vfs-cache-max-size 10G`
- `--vfs-cache-max-age 10m`
- `--dir-cache-time 30s`
- `--poll-interval 0`

## Unraid validation checklist

After installing on Unraid, verify:

1. `http://<unraid-ip>:5573` loads and prompts for login.
2. `/boot/config/go` contains the `RCLONE MANAGER` startup block.
3. `/boot/config/plugins/rclone-mount-manager/manager.env` exists.
4. Creating a remote saves it into the expected `rclone.conf`.
5. Creating a mount starts an `rclone mount` process.
6. The mount path is visible on the host and usable by Plex or other apps.
7. Rebooting Unraid brings the web app back and restores enabled mounts.
8. Deleting a mount removes the saved definition and stops the process.

## Recommended publish layout

Keep one repo with:

- `app.py`
- `templates/`
- `install.sh`
- `install-linux.sh`
- `install-unraid.sh`
- `RCLONE_MANAGER_DEPLOY.md`

That gives users one clone path and one documented install flow while still making the Linux vs Unraid differences clear.
