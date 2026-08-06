# RCLONE MANAGER

Small dependency-free web app for creating and managing persistent `rclone mount`
definitions and `rclone` remotes.

One repo, two platform modes:

- `linux` mode: for normal Linux distros with `systemd`
- `unraid` mode: for Unraid hosts, using persistent files and boot scripts instead of `systemd`

What it does:

- imports existing persistent mounts
- imports existing `rclone` remotes from `rclone.conf`
- creates and edits mount definitions
- creates and edits remote endpoints such as HTTP `url=` targets
- validates remotes before saving so bad backend types are caught early
- tests remote endpoint reachability and `rclone` connectivity from the web UI
- starts, stops, restarts, and deletes mounts
- restarts affected mounts after remote changes
- checks for available updates for both RCLONE MANAGER and `rclone`
- shows the running manager version in the dashboard
- lets the signed-in admin change the dashboard password from the web UI
- separates Dashboard, Updates, and Profile into top tabs for cleaner navigation
- uses Unraid-safe mount defaults when running on Unraid, including explicit config and cache handling

Default port: `5573`

## Install

Clone the repo, then from the repo root run:

```bash
sudo bash install.sh
```

The installer auto-detects the platform:

- on normal Linux, it runs `install-linux.sh`
- on Unraid, it runs `install-unraid.sh`

To set your own login:

```bash
sudo RCLONE_MANAGER_USER=admin RCLONE_MANAGER_PASS='strong-password' bash install.sh
```

## Linux mode

- installs to `/opt/rclone-mount-manager`
- creates `/etc/default/rclone-mount-manager`
- creates `rclone-mount-manager.service`
- manages mounts as `systemd` units

## Unraid mode

- installs to `/boot/config/plugins/rclone-mount-manager`
- stores mount definitions, pid files, and logs in that same persistent area
- auto-creates per-mount VFS cache folders under `/mnt/cache/appdata/rclone-manager`
- updates `/boot/config/go` to start the web app on boot
- waits for `/mnt/user` before auto-starting saved mounts
- manages mounts directly as `rclone mount` processes instead of `systemd`

Unraid notes:

- `systemd` is not used
- host FUSE support is still required for `rclone mount`
- if you use `--allow-other`, your FUSE configuration must permit it
- the default Unraid `rclone.conf` path is auto-detected, but you can override it with `RCLONE_CONFIG_FILE`
- new Unraid mounts automatically add the detected `rclone.conf` path, a per-mount `--cache-dir`, and media-friendly ownership defaults (`uid 99`, `gid 100`, `umask 002`)
- if you leave the cache tuning fields blank on Unraid, the app now fills in safe defaults: `--vfs-cache-mode full`, `--vfs-cache-max-size 10G`, `--vfs-cache-max-age 10m`, `--dir-cache-time 30s`, `--poll-interval 0`

Example Unraid behavior for a new mount created in the UI:

```text
rclone mount media_remote: /mnt/user/Media/LibraryName \
  --config /boot/config/plugins/rclone/.rclone.conf \
  --cache-dir /mnt/cache/appdata/rclone-manager/<service>-vfs-cache \
  --uid 99 --gid 100 --umask 002 \
  --vfs-cache-mode full --vfs-cache-max-size 10G \
  --vfs-cache-max-age 10m --dir-cache-time 30s --poll-interval 0
```

## Update

Pull the latest changes or replace the repo contents, then rerun `install.sh`.

## Source layout

- [app.py](./app.py): shared web app with Linux and Unraid backends
- [install.sh](./install.sh): auto-detecting installer
- [install-linux.sh](./install-linux.sh): standard Linux install
- [install-unraid.sh](./install-unraid.sh): Unraid-specific install
