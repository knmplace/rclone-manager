#!/usr/bin/env python3
"""Browser-based manager for persistent rclone mounts and remotes."""

from __future__ import annotations

import argparse
import base64
import configparser
import html
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from wsgiref.simple_server import WSGIServer, make_server

APP_DIR = Path(__file__).resolve().parent
TEMPLATE_PATH = APP_DIR / "templates" / "index.html"
VERSION_PATH = APP_DIR / "VERSION"
DEFAULT_PORT = int(os.environ.get("RCLONE_MANAGER_PORT", "5573"))
ENV_USER = os.environ.get("RCLONE_MANAGER_USER", "admin")
ENV_PASS = os.environ.get("RCLONE_MANAGER_PASS", "ChangeMeNow!")
PLATFORM = os.environ.get("RCLONE_MANAGER_PLATFORM", "unraid" if Path("/etc/unraid-version").exists() else "linux").lower()

SERVICE_PREFIX = "rclone-"
SERVICE_SUFFIX = ".service"
UNIT_DIR = Path("/etc/systemd/system")

UNRAID_BASE_DIR = Path(os.environ.get("RCLONE_MANAGER_UNRAID_DIR", "/boot/config/plugins/rclone-mount-manager"))
UNRAID_MOUNTS_DIR = UNRAID_BASE_DIR / "mounts"
UNRAID_PIDS_DIR = UNRAID_BASE_DIR / "pids"
UNRAID_LOGS_DIR = UNRAID_BASE_DIR / "logs"


def detect_rclone_config_path() -> Path:
    explicit = os.environ.get("RCLONE_CONFIG_FILE")
    if explicit:
        return Path(explicit)
    if PLATFORM == "unraid":
        for candidate in (
            Path("/boot/config/plugins/rclone/.rclone.conf"),
            Path("/boot/config/rclone/rclone.conf"),
            Path("/boot/config/plugins/rclone/rclone.conf"),
        ):
            if candidate.exists():
                return candidate
        return Path("/boot/config/plugins/rclone/.rclone.conf")
    return Path("/root/.config/rclone/rclone.conf")


RCLONE_CONFIG_PATH = detect_rclone_config_path()
MANAGER_RELEASE_API = "https://api.github.com/repos/knmplace/rclone-manager/releases/latest"
RCLONE_RELEASE_API = "https://api.github.com/repos/rclone/rclone/releases/latest"
RCLONE_BIN = shutil.which("rclone") or "/usr/bin/rclone"
PYTHON_BIN = shutil.which("python3") or shutil.which("python") or sys.executable
UPDATE_LOG_PATH = (UNRAID_LOGS_DIR if PLATFORM == "unraid" else APP_DIR) / "manager-update.log"
RCLONE_UPDATE_LOG_PATH = (UNRAID_LOGS_DIR if PLATFORM == "unraid" else APP_DIR) / "rclone-update.log"
PROFILE_UPDATE_LOG_PATH = (UNRAID_LOGS_DIR if PLATFORM == "unraid" else APP_DIR) / "profile-update.log"
LINUX_MANAGER_ENV_PATH = Path("/etc/default/rclone-mount-manager")
UNRAID_MANAGER_ENV_PATH = UNRAID_BASE_DIR / "manager.env"
UNRAID_START_MANAGER = UNRAID_BASE_DIR / "start-manager.sh"
UNRAID_STOP_MANAGER = UNRAID_BASE_DIR / "stop-manager.sh"


def detect_fusermount_command() -> list[str]:
    for binary in ("fusermount3", "fusermount"):
        resolved = shutil.which(binary)
        if resolved:
            return [resolved]
    for candidate in ("/usr/bin/fusermount3", "/bin/fusermount3", "/usr/bin/fusermount", "/bin/fusermount"):
        if Path(candidate).exists():
            return [candidate]
    return ["umount", "-l"]


FUSERMOUNT_COMMAND = detect_fusermount_command()


def read_manager_version() -> str:
    if VERSION_PATH.exists():
        return VERSION_PATH.read_text(encoding="utf-8").strip() or "unknown"
    return "unknown"


def normalize_version(version: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", version or "")
    if not parts:
        return (0,)
    return tuple(int(part) for part in parts[:4])


def version_is_newer(latest: str, current: str) -> bool:
    return normalize_version(latest) > normalize_version(current)


def fetch_json(url: str) -> dict[str, Any]:
    request = Request(url, headers={"User-Agent": "RCLONE-MANAGER", "Accept": "application/vnd.github+json"})
    try:
        with urlopen(request, timeout=15) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise AppError(f"Update check failed: HTTP {exc.code} from release API", 502) from exc
    except URLError as exc:
        raise AppError(f"Update check failed: {exc.reason}", 502) from exc


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def shell_quote_env(value: str) -> str:
    return shlex.quote(str(value))


class UpdateManager:
    def __init__(self, manager: "MountManager"):
        self.manager = manager

    def check(self) -> dict[str, Any]:
        manager_release = fetch_json(MANAGER_RELEASE_API)
        rclone_release = fetch_json(RCLONE_RELEASE_API)
        manager_asset = self._select_manager_asset(manager_release)
        current_manager = read_manager_version()
        current_rclone = self._read_rclone_version()
        latest_manager = str(manager_release.get("tag_name", "")).strip() or "unknown"
        latest_rclone = str(rclone_release.get("tag_name", "")).strip() or "unknown"
        return {
            "platform": PLATFORM,
            "manager": {
                "current_version": current_manager,
                "latest_version": latest_manager,
                "update_available": version_is_newer(latest_manager, current_manager),
                "can_update": bool(manager_asset),
                "asset_url": manager_asset.get("browser_download_url", "") if manager_asset else "",
                "release_url": str(manager_release.get("html_url", "")).strip(),
                "log_path": str(UPDATE_LOG_PATH),
            },
            "rclone": {
                "current_version": current_rclone,
                "latest_version": latest_rclone,
                "update_available": version_is_newer(latest_rclone, current_rclone),
                "can_update": bool(shutil.which("rclone") or Path(RCLONE_BIN).exists()),
                "release_url": str(rclone_release.get("html_url", "")).strip(),
                "log_path": str(RCLONE_UPDATE_LOG_PATH),
            },
        }

    def start_manager_update(self) -> dict[str, Any]:
        status = self.check()["manager"]
        if not status["asset_url"]:
            raise AppError("No downloadable manager release asset was found.", 500)
        script = self._build_manager_update_script(status["asset_url"])
        self._spawn_shell(script, UPDATE_LOG_PATH)
        return {
            "status": "started",
            "message": "Manager update started. The web UI will briefly restart when the installer runs.",
            "log_path": str(UPDATE_LOG_PATH),
        }

    def start_rclone_update(self) -> dict[str, Any]:
        rclone_path = shutil.which("rclone") or RCLONE_BIN
        if not rclone_path or not Path(rclone_path).exists():
            raise AppError("rclone was not found on this system.", 404)
        script = self._build_rclone_update_script(rclone_path)
        self._spawn_shell(script, RCLONE_UPDATE_LOG_PATH)
        return {
            "status": "started",
            "message": "rclone update started. Active mounts will be restarted after the binary update completes.",
            "log_path": str(RCLONE_UPDATE_LOG_PATH),
        }

    def _read_rclone_version(self) -> str:
        rclone_path = shutil.which("rclone") or RCLONE_BIN
        if not rclone_path or not Path(rclone_path).exists():
            return "not installed"
        result = subprocess.run([rclone_path, "version"], capture_output=True, text=True)
        text = (result.stdout or result.stderr or "").strip()
        match = re.search(r"rclone\s+(v[0-9][^\s]*)", text, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        first_line = text.splitlines()[0].strip() if text else "unknown"
        return first_line or "unknown"

    def _select_manager_asset(self, release: dict[str, Any]) -> dict[str, Any] | None:
        assets = release.get("assets") or []
        for asset in assets:
            name = str(asset.get("name", ""))
            if name.startswith("RCLONE-MANAGER-") and name.endswith(".zip"):
                return asset
        return None

    def _build_manager_update_script(self, asset_url: str) -> str:
        temp_root = tempfile.mkdtemp(prefix="rclone-manager-update-")
        extract_dir = Path(temp_root) / "src"
        app_root = UNRAID_BASE_DIR if PLATFORM == "unraid" else APP_DIR
        env_pairs = {
            "APP_ROOT": str(app_root),
            "RCLONE_MANAGER_PORT": str(DEFAULT_PORT),
            "RCLONE_MANAGER_USER": ENV_USER,
            "RCLONE_MANAGER_PASS": ENV_PASS,
            "RCLONE_CONFIG_FILE": str(RCLONE_CONFIG_PATH),
        }
        env_prefix = " ".join(f"{key}={shell_quote_env(value)}" for key, value in env_pairs.items())
        return "\n".join(
            [
                "set -e",
                f"TMPDIR={shell_quote_env(temp_root)}",
                f"ZIPFILE={shell_quote_env(str(Path(temp_root) / 'manager.zip'))}",
                f"EXTRACTDIR={shell_quote_env(str(extract_dir))}",
                "mkdir -p \"$EXTRACTDIR\"",
                f"python3 - <<'PY'\nimport urllib.request\nurllib.request.urlretrieve({asset_url!r}, {str(Path(temp_root) / 'manager.zip')!r})\nPY",
                "python3 - <<'PY'\nimport zipfile\nzipfile.ZipFile(" + repr(str(Path(temp_root) / "manager.zip")) + ").extractall(" + repr(str(extract_dir)) + ")\nPY",
                f"cd {shell_quote_env(str(extract_dir))}",
                f"{env_prefix} bash ./install.sh",
            ]
        )

    def _build_rclone_update_script(self, rclone_path: str) -> str:
        env_pairs = {
            "RCLONE_MANAGER_PLATFORM": PLATFORM,
            "RCLONE_MANAGER_PORT": str(DEFAULT_PORT),
            "RCLONE_MANAGER_USER": ENV_USER,
            "RCLONE_MANAGER_PASS": ENV_PASS,
            "RCLONE_MANAGER_UNRAID_DIR": str(UNRAID_BASE_DIR),
            "RCLONE_CONFIG_FILE": str(RCLONE_CONFIG_PATH),
        }
        env_prefix = " ".join(f"{key}={shell_quote_env(value)}" for key, value in env_pairs.items())
        quoted_python = shell_quote_env(PYTHON_BIN)
        quoted_app = shell_quote_env(str(APP_DIR / "app.py"))
        quoted_rclone = shell_quote_env(rclone_path)
        return "\n".join(
            [
                "set -e",
                f"{quoted_rclone} selfupdate",
                f"{env_prefix} {quoted_python} {quoted_app} --stop-all || true",
                "sleep 2",
                f"{env_prefix} {quoted_python} {quoted_app} --start-all || true",
            ]
        )

    def _spawn_shell(self, script: str, log_path: Path) -> None:
        ensure_parent(log_path)
        with log_path.open("ab") as handle:
            subprocess.Popen(
                ["bash", "-lc", script],
                stdout=handle,
                stderr=handle,
                start_new_session=True,
                close_fds=True,
            )


class ProfileManager:
    def get_profile(self) -> dict[str, Any]:
        return {"username": ENV_USER, "version": read_manager_version(), "platform": PLATFORM}

    def change_password(self, payload: dict[str, Any]) -> dict[str, Any]:
        new_password = str(payload.get("new_password", ""))
        confirm_password = str(payload.get("confirm_password", ""))
        if len(new_password) < 8:
            raise AppError("New password must be at least 8 characters.", 400)
        if new_password != confirm_password:
            raise AppError("New password and confirmation do not match.", 400)
        self._write_manager_password(new_password)
        self._spawn_restart()
        return {
            "status": "started",
            "message": "Password updated. The manager will restart briefly, then sign in again with the new password.",
            "log_path": str(PROFILE_UPDATE_LOG_PATH),
        }

    def _write_manager_password(self, new_password: str) -> None:
        env_path = UNRAID_MANAGER_ENV_PATH if PLATFORM == "unraid" else LINUX_MANAGER_ENV_PATH
        if not env_path.exists():
            raise AppError(f"Manager environment file was not found: {env_path}", 500)
        lines = env_path.read_text(encoding="utf-8").splitlines()
        updated = False
        new_lines: list[str] = []
        for line in lines:
            if line.startswith("RCLONE_MANAGER_PASS="):
                new_lines.append(f"RCLONE_MANAGER_PASS={new_password}")
                updated = True
            else:
                new_lines.append(line)
        if not updated:
            new_lines.append(f"RCLONE_MANAGER_PASS={new_password}")
        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    def _spawn_restart(self) -> None:
        ensure_parent(PROFILE_UPDATE_LOG_PATH)
        if PLATFORM == "unraid":
            script = f"sleep 1; bash {shell_quote_env(str(UNRAID_STOP_MANAGER))} || true; sleep 1; bash {shell_quote_env(str(UNRAID_START_MANAGER))}"
        else:
            script = "sleep 1; systemctl restart rclone-mount-manager.service"
        with PROFILE_UPDATE_LOG_PATH.open("ab") as handle:
            subprocess.Popen(
                ["bash", "-lc", script],
                stdout=handle,
                stderr=handle,
                start_new_session=True,
                close_fds=True,
            )


class ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True


class AppError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class MountBackend:
    managed_marker = "# Managed by RCLONE MANAGER"
    known_value_flags = {
        "--dir-cache-time",
        "--vfs-cache-mode",
        "--vfs-cache-max-age",
        "--poll-interval",
        "--buffer-size",
        "--vfs-read-ahead",
    }
    known_bool_flags = {"--allow-other", "--read-only"}

    def list_mounts(self, remote_map: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
        raise NotImplementedError

    def get_mount(self, service_name: str, remote_map: dict[str, dict[str, str]]) -> dict[str, Any]:
        for mount in self.list_mounts(remote_map):
            if mount["service_name"] == service_name:
                return mount
        raise AppError(f"Mount service '{service_name}' not found", 404)

    def save_mount(self, data: dict[str, Any], remote_map: dict[str, dict[str, str]]) -> dict[str, Any]:
        raise NotImplementedError

    def delete_mount(self, service_name: str, remote_map: dict[str, dict[str, str]]) -> None:
        raise NotImplementedError

    def control_mount(self, service_name: str, action: str, remote_map: dict[str, dict[str, str]]) -> dict[str, Any]:
        raise NotImplementedError

    def restart_mounts_for_remote(self, remote_name: str, remote_map: dict[str, dict[str, str]]) -> list[str]:
        restarted = []
        for mount in self.list_mounts(remote_map):
            if mount.get("remote_name") == remote_name and (mount["enabled"] or mount["state"] == "active"):
                self.control_mount(mount["service_name"], "restart", remote_map)
                restarted.append(mount["service_name"])
        return restarted

    def start_all(self, remote_map: dict[str, dict[str, str]]) -> list[str]:
        started = []
        for mount in self.list_mounts(remote_map):
            if mount["enabled"]:
                self.control_mount(mount["service_name"], "start", remote_map)
                started.append(mount["service_name"])
        return started

    def stop_all(self, remote_map: dict[str, dict[str, str]]) -> list[str]:
        stopped = []
        for mount in self.list_mounts(remote_map):
            if mount["state"] == "active":
                self.control_mount(mount["service_name"], "stop", remote_map)
                stopped.append(mount["service_name"])
        return stopped

    def _split_args(self, args: list[str]) -> tuple[dict[str, Any], list[str]]:
        known: dict[str, Any] = {
            "allow_other": False,
            "read_only": False,
            "dir_cache_time": "",
            "vfs_cache_mode": "",
            "vfs_cache_max_age": "",
            "poll_interval": "",
            "buffer_size": "",
            "vfs_read_ahead": "",
        }
        extra: list[str] = []
        idx = 0
        while idx < len(args):
            token = args[idx]
            if token in self.known_bool_flags:
                known["allow_other" if token == "--allow-other" else "read_only"] = True
                idx += 1
                continue
            if token in self.known_value_flags and idx + 1 < len(args):
                known[token[2:].replace("-", "_")] = args[idx + 1]
                idx += 2
                continue
            extra.append(token)
            idx += 1
        return known, extra

    @staticmethod
    def _run(command: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(command, capture_output=True, text=True)
        if check and result.returncode != 0:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            raise AppError(stderr or stdout or f"command failed: {' '.join(command)}", 500)
        return result

    def _unmount_path(self, mount_point: str, *, lazy: bool = True) -> None:
        if FUSERMOUNT_COMMAND[:1] and Path(FUSERMOUNT_COMMAND[0]).name.startswith("fuser"):
            flag = "-uz" if lazy else "-u"
            self._run([*FUSERMOUNT_COMMAND, flag, mount_point], check=False)
            return
        command = [*FUSERMOUNT_COMMAND]
        if lazy:
            command.append("-l")
        command.append(mount_point)
        self._run(command, check=False)


class SystemdBackend(MountBackend):
    @staticmethod
    def _unit_name(service_name: str) -> str:
        return f"{SERVICE_PREFIX}{service_name}{SERVICE_SUFFIX}"

    def list_mounts(self, remote_map: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
        result = self._run(
            ["systemctl", "list-unit-files", "rclone-*.service", "--type=service", "--no-legend", "--plain"],
            check=False,
        )
        mounts = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if not parts:
                continue
            unit_name = parts[0].strip()
            if unit_name in {"rclone-rcd.service", "rclone-mount-manager.service"}:
                continue
            parsed = self._read_mount_service(unit_name, remote_map)
            if parsed:
                mounts.append(parsed)
        return sorted(mounts, key=lambda item: item["service_name"])

    def save_mount(self, data: dict[str, Any], remote_map: dict[str, dict[str, str]]) -> dict[str, Any]:
        unit_name = self._unit_name(data["service_name"])
        unit_path = UNIT_DIR / unit_name
        if data.get("_creating") and unit_path.exists():
            raise AppError(f"Service '{data['service_name']}' already exists", 409)
        unit_path.write_text(self._render_unit_file(data), encoding="utf-8")
        self._run(["systemctl", "daemon-reload"])
        self._run(["systemctl", "enable", unit_name])
        self._run(["systemctl", "restart", unit_name])
        return self.get_mount(data["service_name"], remote_map)

    def delete_mount(self, service_name: str, remote_map: dict[str, dict[str, str]]) -> None:
        mount = self.get_mount(service_name, remote_map)
        unit_path = UNIT_DIR / mount["unit_name"]
        self._run(["systemctl", "disable", "--now", mount["unit_name"]], check=False)
        if unit_path.exists():
            unit_path.unlink()
        self._run(["systemctl", "daemon-reload"])
        self._unmount_path(mount["mount_point"], lazy=True)

    def control_mount(self, service_name: str, action: str, remote_map: dict[str, dict[str, str]]) -> dict[str, Any]:
        if action not in {"start", "stop", "restart"}:
            raise AppError(f"Unsupported action '{action}'", 400)
        self._run(["systemctl", action, self._unit_name(service_name)])
        return self.get_mount(service_name, remote_map)

    def _read_mount_service(self, unit_name: str, remote_map: dict[str, dict[str, str]]) -> dict[str, Any] | None:
        unit_path = UNIT_DIR / unit_name
        if not unit_path.exists():
            return None
        unit_text = unit_path.read_text(encoding="utf-8")
        if "rclone mount" not in unit_text:
            return None
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.read_string(unit_text)
        exec_start = parser["Service"].get("ExecStart")
        description = parser["Unit"].get("Description", "")
        if not exec_start:
            raise AppError(f"Invalid service file for {unit_name}: missing ExecStart", 500)
        args = shlex.split(exec_start)
        mount_idx = args.index("mount")
        remote = args[mount_idx + 1]
        mount_point = args[mount_idx + 2]
        flag_args = args[mount_idx + 3 :]
        known, extra = self._split_args(flag_args)
        remote_name = remote[:-1] if remote.endswith(":") else remote
        remote_config = remote_map.get(remote_name, {})
        state = self._run(["systemctl", "is-active", unit_name], check=False).stdout.strip() or "unknown"
        enabled = self._run(["systemctl", "is-enabled", unit_name], check=False).returncode == 0
        service_name = unit_name[len(SERVICE_PREFIX) : -len(SERVICE_SUFFIX)]
        return {
            "service_name": service_name,
            "unit_name": unit_name,
            "description": description,
            "remote": remote,
            "remote_name": remote_name,
            "remote_details": {
                "type": remote_config.get("type", ""),
                "url": remote_config.get("url", ""),
                "no_head": remote_config.get("no_head", ""),
            },
            "mount_point": mount_point,
            "state": state,
            "enabled": enabled,
            "raw_args": flag_args,
            "raw_args_text": " ".join(shlex.quote(arg) for arg in flag_args),
            "options": known,
            "extra_args_text": " ".join(shlex.quote(arg) for arg in extra),
            "managed": self.managed_marker in unit_text,
        }

    def _render_unit_file(self, data: dict[str, Any]) -> str:
        quoted_mount = shlex.quote(data["mount_point"])
        command_parts = ["/usr/bin/rclone", "mount", data["remote"], data["mount_point"], *data["args"]]
        exec_start = " ".join(shlex.quote(part) for part in command_parts)
        return (
            f"{self.managed_marker}\n"
            "[Unit]\n"
            f"Description={data['description']}\n"
            "After=network-online.target\n"
            "Wants=network-online.target\n\n"
            "[Service]\n"
            "Type=notify\n"
            f"ExecStartPre=/bin/mkdir -p {quoted_mount}\n"
            f"ExecStart={exec_start}\n"
            f"ExecStop=/bin/sh -c {shlex.quote(' '.join(shlex.quote(part) for part in [*FUSERMOUNT_COMMAND, '-u', data['mount_point']]))}\n"
            "Restart=on-failure\n"
            "RestartSec=10\n\n"
            "[Install]\n"
            "WantedBy=multi-user.target\n"
        )


class UnraidBackend(MountBackend):
    def __init__(self) -> None:
        for path in (UNRAID_MOUNTS_DIR, UNRAID_PIDS_DIR, UNRAID_LOGS_DIR):
            path.mkdir(parents=True, exist_ok=True)

    def list_mounts(self, remote_map: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
        mounts = []
        for file_path in sorted(UNRAID_MOUNTS_DIR.glob("*.json")):
            mounts.append(self._mount_record(file_path, remote_map))
        return mounts

    def save_mount(self, data: dict[str, Any], remote_map: dict[str, dict[str, str]]) -> dict[str, Any]:
        file_path = self._mount_file(data["service_name"])
        if data.get("_creating") and file_path.exists():
            raise AppError(f"Mount '{data['service_name']}' already exists", 409)
        persisted = {
            "service_name": data["service_name"],
            "description": data["description"],
            "remote": data["remote"],
            "mount_point": data["mount_point"],
            "args": data["args"],
            "enabled": True,
        }
        file_path.write_text(json.dumps(persisted, indent=2), encoding="utf-8")
        if self._is_active(data["service_name"]):
            self._restart(data["service_name"])
        else:
            self._start(data["service_name"])
        return self.get_mount(data["service_name"], remote_map)

    def delete_mount(self, service_name: str, remote_map: dict[str, dict[str, str]]) -> None:
        self._stop(service_name)
        file_path = self._mount_file(service_name)
        if file_path.exists():
            file_path.unlink()
        pid_file = self._pid_file(service_name)
        if pid_file.exists():
            pid_file.unlink()

    def control_mount(self, service_name: str, action: str, remote_map: dict[str, dict[str, str]]) -> dict[str, Any]:
        if action == "start":
            self._start(service_name)
        elif action == "stop":
            self._stop(service_name)
        elif action == "restart":
            self._restart(service_name)
        else:
            raise AppError(f"Unsupported action '{action}'", 400)
        return self.get_mount(service_name, remote_map)

    def _mount_record(self, file_path: Path, remote_map: dict[str, dict[str, str]]) -> dict[str, Any]:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        service_name = data["service_name"]
        raw_args = list(data.get("args", []))
        known, extra = self._split_args(raw_args)
        remote = data["remote"]
        remote_name = remote[:-1] if remote.endswith(":") else remote
        remote_config = remote_map.get(remote_name, {})
        state = "active" if self._is_active(service_name) else "inactive"
        return {
            "service_name": service_name,
            "unit_name": f"unraid:{service_name}",
            "description": data.get("description", ""),
            "remote": remote,
            "remote_name": remote_name,
            "remote_details": {
                "type": remote_config.get("type", ""),
                "url": remote_config.get("url", ""),
                "no_head": remote_config.get("no_head", ""),
            },
            "mount_point": data["mount_point"],
            "state": state,
            "enabled": bool(data.get("enabled", True)),
            "raw_args": raw_args,
            "raw_args_text": " ".join(shlex.quote(arg) for arg in raw_args),
            "options": known,
            "extra_args_text": " ".join(shlex.quote(arg) for arg in extra),
            "managed": True,
        }

    def _start(self, service_name: str) -> None:
        data = self._load_mount_config(service_name)
        mount_dir = Path(data["mount_point"])
        mount_dir.mkdir(parents=True, exist_ok=True)
        if self._is_active(service_name):
            return
        log_path = self._log_file(service_name)
        with log_path.open("ab") as log_handle:
            process = subprocess.Popen(
                ["/usr/bin/rclone", "mount", data["remote"], data["mount_point"], *data.get("args", [])],
                stdout=log_handle,
                stderr=log_handle,
                preexec_fn=os.setpgrp,
                close_fds=True,
            )
        self._pid_file(service_name).write_text(str(process.pid), encoding="utf-8")
        time.sleep(1)
        if process.poll() is not None:
            message = self._tail_log(service_name) or f"rclone exited immediately for {service_name}"
            raise AppError(message, 500)

    def _stop(self, service_name: str) -> None:
        pid = self._read_pid(service_name)
        mount = self._load_mount_config(service_name)
        self._unmount_path(mount["mount_point"], lazy=True)
        if pid:
            try:
                os.killpg(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            for _ in range(15):
                if not self._pid_alive(pid):
                    break
                time.sleep(0.2)
            if self._pid_alive(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        pid_file = self._pid_file(service_name)
        if pid_file.exists():
            pid_file.unlink()

    def _restart(self, service_name: str) -> None:
        self._stop(service_name)
        self._start(service_name)

    def _load_mount_config(self, service_name: str) -> dict[str, Any]:
        file_path = self._mount_file(service_name)
        if not file_path.exists():
            raise AppError(f"Mount '{service_name}' not found", 404)
        return json.loads(file_path.read_text(encoding="utf-8"))

    def _is_active(self, service_name: str) -> bool:
        pid = self._read_pid(service_name)
        mount_file = self._mount_file(service_name)
        mount_point = None
        if mount_file.exists():
            try:
                mount_point = json.loads(mount_file.read_text(encoding="utf-8")).get("mount_point")
            except Exception:
                mount_point = None
        mounted = bool(mount_point) and self._run(["/bin/mountpoint", "-q", mount_point], check=False).returncode == 0
        if pid and self._pid_alive(pid):
            return True
        return mounted

    def _read_pid(self, service_name: str) -> int | None:
        pid_file = self._pid_file(service_name)
        if not pid_file.exists():
            return None
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except ValueError:
            return None
        return pid if self._pid_alive(pid) else None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    @staticmethod
    def _mount_file(service_name: str) -> Path:
        return UNRAID_MOUNTS_DIR / f"{service_name}.json"

    @staticmethod
    def _pid_file(service_name: str) -> Path:
        return UNRAID_PIDS_DIR / f"{service_name}.pid"

    @staticmethod
    def _log_file(service_name: str) -> Path:
        return UNRAID_LOGS_DIR / f"{service_name}.log"

    def _tail_log(self, service_name: str) -> str:
        log_path = self._log_file(service_name)
        if not log_path.exists():
            return ""
        lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        return "\n".join(lines[-10:]).strip()


class MountManager:
    service_name_re = re.compile(r"^[a-zA-Z0-9._-]+$")
    remote_name_re = re.compile(r"^[a-zA-Z0-9._-]+$")

    def __init__(self, backend: MountBackend):
        self.backend = backend

    def list_mounts(self) -> list[dict[str, Any]]:
        return self.backend.list_mounts(self._read_remote_map())

    def list_remotes(self) -> list[dict[str, Any]]:
        remote_map = self._read_remote_map()
        mounts = self.list_mounts()
        return [self._remote_record(name, config, mounts) for name, config in sorted(remote_map.items())]

    def get_mount(self, service_name: str) -> dict[str, Any]:
        return self.backend.get_mount(service_name, self._read_remote_map())

    def save_mount(self, payload: dict[str, Any], *, existing: str | None = None) -> dict[str, Any]:
        data = self._normalize_mount_payload(payload, existing=existing)
        data["_creating"] = existing is None
        return self.backend.save_mount(data, self._read_remote_map())

    def delete_mount(self, service_name: str) -> None:
        self.backend.delete_mount(service_name, self._read_remote_map())

    def control_mount(self, service_name: str, action: str) -> dict[str, Any]:
        return self.backend.control_mount(service_name, action, self._read_remote_map())

    def get_remote(self, name: str) -> dict[str, Any]:
        remote_map = self._read_remote_map()
        if name not in remote_map:
            raise AppError(f"Remote '{name}' not found", 404)
        return self._remote_record(name, remote_map[name], self.list_mounts())

    def test_remote(self, payload: dict[str, Any], *, existing: str | None = None) -> dict[str, Any]:
        data = self._normalize_remote_payload(payload, existing=existing)
        return self._run_remote_checks(data)

    def save_remote(self, payload: dict[str, Any], *, existing: str | None = None) -> dict[str, Any]:
        data = self._normalize_remote_payload(payload, existing=existing)
        validation = self._run_remote_checks(data)
        if not validation.get("rclone", {}).get("ok"):
            message = validation["rclone"].get("message") or "Remote validation failed."
            raise AppError(message, 400)
        parser = self._read_remote_parser()
        if existing is None and parser.has_section(data["name"]):
            raise AppError(f"Remote '{data['name']}' already exists", 409)
        if not parser.has_section(data["name"]):
            parser.add_section(data["name"])
        for key in list(parser[data["name"]].keys()):
            parser.remove_option(data["name"], key)
        for key, value in data["config"].items():
            parser[data["name"]][key] = value
        self._write_remote_parser(parser)
        restarted = self.backend.restart_mounts_for_remote(data["name"], self._read_remote_map())
        remote = self.get_remote(data["name"])
        remote["restarted_mounts"] = restarted
        remote["validation"] = validation
        return remote

    def delete_remote(self, name: str) -> None:
        mounts = [mount for mount in self.list_mounts() if mount.get("remote_name") == name]
        if mounts:
            raise AppError(f"Remote '{name}' is still used by: {', '.join(m['service_name'] for m in mounts)}", 409)
        parser = self._read_remote_parser()
        if not parser.remove_section(name):
            raise AppError(f"Remote '{name}' not found", 404)
        self._write_remote_parser(parser)

    def start_all_mounts(self) -> list[str]:
        return self.backend.start_all(self._read_remote_map())

    def stop_all_mounts(self) -> list[str]:
        return self.backend.stop_all(self._read_remote_map())

    def _remote_record(self, name: str, config: dict[str, str], mounts: list[dict[str, Any]]) -> dict[str, Any]:
        related = [mount for mount in mounts if mount.get("remote_name") == name]
        extra = {key: value for key, value in config.items() if key not in {"type", "url", "no_head"}}
        return {
            "name": name,
            "alias": f"{name}:",
            "type": config.get("type", ""),
            "url": config.get("url", ""),
            "no_head": config.get("no_head", ""),
            "config": dict(config),
            "extra_config_lines": "\n".join(f"{key}={value}" for key, value in sorted(extra.items())),
            "mounts": [{"service_name": mount["service_name"], "mount_point": mount["mount_point"], "state": mount["state"]} for mount in related],
        }

    def _normalize_mount_payload(self, payload: dict[str, Any], *, existing: str | None) -> dict[str, Any]:
        service_name = str(payload.get("service_name", "")).strip()
        if existing and service_name != existing:
            raise AppError("Renaming an existing service is not supported yet. Create a new mount instead.")
        if not self.service_name_re.fullmatch(service_name):
            raise AppError("Service name may contain only letters, numbers, dot, underscore, and dash.")
        remote = str(payload.get("remote", "")).strip()
        if not remote.endswith(":"):
            raise AppError("Remote must look like remote_name:")
        remote_name = remote[:-1]
        if remote_name not in self._read_remote_map():
            raise AppError(f"Remote '{remote_name}' does not exist yet. Create or fix the remote first.")
        mount_point = str(payload.get("mount_point", "")).strip()
        if not mount_point.startswith("/"):
            raise AppError("Mount point must be an absolute Linux path.")
        description = str(payload.get("description", "")).strip() or f"rclone mount {service_name}"
        options = payload.get("options") or {}
        if not isinstance(options, dict):
            raise AppError("Options must be a JSON object.")
        args: list[str] = []
        if _truthy(options.get("allow_other")):
            args.append("--allow-other")
        if _truthy(options.get("read_only")):
            args.append("--read-only")
        for field in ("dir_cache_time", "vfs_cache_mode", "vfs_cache_max_age", "poll_interval", "buffer_size", "vfs_read_ahead"):
            value = str(options.get(field, "")).strip()
            if value:
                args.extend([f"--{field.replace('_', '-')}", value])
        extra_args_text = str(payload.get("extra_args_text", "")).strip()
        if extra_args_text:
            try:
                args.extend(shlex.split(extra_args_text))
            except ValueError as exc:
                raise AppError(f"Could not parse extra arguments: {exc}") from exc
        return {"service_name": service_name, "description": description, "remote": remote, "mount_point": mount_point, "args": args}

    def _normalize_remote_payload(self, payload: dict[str, Any], *, existing: str | None) -> dict[str, Any]:
        name = str(payload.get("name", "")).strip()
        if existing and name != existing:
            raise AppError("Renaming an existing remote is not supported yet. Create a new one instead.")
        if not self.remote_name_re.fullmatch(name):
            raise AppError("Remote name may contain only letters, numbers, dot, underscore, and dash.")
        remote_type = str(payload.get("type", "")).strip() or "http"
        url = str(payload.get("url", "")).strip()
        if remote_type == "http" and not url:
            raise AppError("HTTP remotes require a URL.")
        config = {"type": remote_type}
        if url:
            config["url"] = url
        no_head = payload.get("no_head", "")
        if str(no_head).strip():
            config["no_head"] = str(no_head).strip().lower()
        extra_lines = str(payload.get("extra_config_lines", "")).strip()
        if extra_lines:
            for raw_line in extra_lines.splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    raise AppError(f"Extra remote config line must be key=value: {line}")
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip()
                if not key:
                    raise AppError(f"Remote config key cannot be empty: {line}")
                config[key] = value
        return {"name": name, "config": config}

    def _read_remote_parser(self) -> configparser.ConfigParser:
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        if RCLONE_CONFIG_PATH.exists():
            parser.read(RCLONE_CONFIG_PATH, encoding="utf-8")
        return parser

    def _read_remote_map(self) -> dict[str, dict[str, str]]:
        parser = self._read_remote_parser()
        return {section: dict(parser[section].items()) for section in parser.sections()}

    def _write_remote_parser(self, parser: configparser.ConfigParser) -> None:
        RCLONE_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RCLONE_CONFIG_PATH.open("w", encoding="utf-8") as handle:
            parser.write(handle, space_around_delimiters=False)

    def _run_remote_checks(self, data: dict[str, Any]) -> dict[str, Any]:
        endpoint = self._probe_remote_endpoint(data)
        rclone = self._probe_rclone_remote(data)
        return {"name": data["name"], "type": data["config"].get("type", ""), "endpoint": endpoint, "rclone": rclone}

    def _probe_remote_endpoint(self, data: dict[str, Any]) -> dict[str, Any]:
        remote_type = data["config"].get("type", "").strip().lower()
        url = data["config"].get("url", "").strip()
        if remote_type not in {"http", "webdav"} or not url:
            return {"ok": None, "status_code": None, "message": "Endpoint probe skipped for this remote type."}
        method = "PROPFIND" if remote_type == "webdav" else "GET"
        request = Request(url, method=method, headers={"User-Agent": "RCLONE-MANAGER"})
        try:
            with urlopen(request, timeout=10) as response:
                status_code = getattr(response, "status", 200)
                ok = status_code in ({200, 207} if remote_type == "webdav" else {200})
                return {
                    "ok": ok,
                    "status_code": status_code,
                    "message": f"Endpoint responded with HTTP {status_code}.",
                }
        except HTTPError as exc:
            status_code = int(exc.code)
            ok = status_code in ({200, 207} if remote_type == "webdav" else {200})
            return {
                "ok": ok,
                "status_code": status_code,
                "message": f"Endpoint responded with HTTP {status_code}.",
            }
        except URLError as exc:
            return {"ok": False, "status_code": None, "message": f"Endpoint probe failed: {exc.reason}"}

    def _probe_rclone_remote(self, data: dict[str, Any]) -> dict[str, Any]:
        rclone_path = shutil.which("rclone") or RCLONE_BIN
        if not rclone_path or not Path(rclone_path).exists():
            return {"ok": False, "message": "rclone is not installed on this host."}
        parser = configparser.ConfigParser(interpolation=None)
        parser.optionxform = str
        parser.add_section(data["name"])
        for key, value in data["config"].items():
            parser[data["name"]][key] = value
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".conf") as handle:
            parser.write(handle, space_around_delimiters=False)
            temp_config = Path(handle.name)
        try:
            result = subprocess.run(
                [rclone_path, "lsd", f"{data['name']}:", "--config", str(temp_config), "--contimeout", "10s", "--timeout", "15s", "--retries", "1"],
                capture_output=True,
                text=True,
            )
        finally:
            temp_config.unlink(missing_ok=True)
        if result.returncode == 0:
            return {"ok": True, "message": "rclone validated the remote successfully."}
        error_text = (result.stderr or result.stdout or "").strip()
        return {"ok": False, "message": error_text or "rclone validation failed."}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return bool(value)


def read_body_json(environ: dict[str, Any]) -> dict[str, Any]:
    try:
        length = int(environ.get("CONTENT_LENGTH", "0") or "0")
    except ValueError:
        length = 0
    raw = environ["wsgi.input"].read(length) if length else b"{}"
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        raise AppError(f"Invalid JSON body: {exc}") from exc


def json_response(start_response, payload: dict[str, Any], status: int = 200):
    body = json.dumps(payload).encode("utf-8")
    status_text = {
        200: "200 OK",
        201: "201 Created",
        202: "202 Accepted",
        400: "400 Bad Request",
        401: "401 Unauthorized",
        404: "404 Not Found",
        409: "409 Conflict",
        500: "500 Internal Server Error",
    }.get(status, f"{status} Error")
    headers = [("Content-Type", "application/json"), ("Content-Length", str(len(body))), ("Cache-Control", "no-store")]
    if status == 401:
        headers.append(("WWW-Authenticate", 'Basic realm="RCLONE MANAGER"'))
    start_response(status_text, headers)
    return [body]


def html_response(start_response, body_text: str):
    body = body_text.encode("utf-8")
    start_response("200 OK", [("Content-Type", "text/html; charset=utf-8"), ("Content-Length", str(len(body))), ("Cache-Control", "no-store")])
    return [body]


def authorize(environ: dict[str, Any]) -> bool:
    auth = environ.get("HTTP_AUTHORIZATION", "")
    if not auth.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(auth.split(" ", 1)[1]).decode("utf-8")
    except Exception:
        return False
    username, _, password = raw.partition(":")
    return username == ENV_USER and password == ENV_PASS


def render_index() -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    return (
        template.replace("{{DEFAULT_USER}}", html.escape(ENV_USER))
        .replace("{{DEFAULT_PORT}}", str(DEFAULT_PORT))
        .replace("{{PLATFORM}}", html.escape(PLATFORM))
        .replace("{{CURRENT_MANAGER_VERSION}}", html.escape(read_manager_version()))
    )


def build_manager() -> MountManager:
    backend: MountBackend = UnraidBackend() if PLATFORM == "unraid" else SystemdBackend()
    return MountManager(backend)


def create_app() -> Any:
    manager = build_manager()
    updater = UpdateManager(manager)
    profile = ProfileManager()

    def app(environ, start_response):
        try:
            if not authorize(environ):
                return json_response(start_response, {"error": "Authentication required"}, 401)
            method = environ["REQUEST_METHOD"]
            path = environ.get("PATH_INFO", "/")
            if path == "/" and method == "GET":
                return html_response(start_response, render_index())
            if path == "/api/health" and method == "GET":
                mounts = manager.list_mounts()
                remotes = manager.list_remotes()
                return json_response(start_response, {"status": "ok", "port": DEFAULT_PORT, "user": ENV_USER, "mounts": len(mounts), "remotes": len(remotes), "platform": PLATFORM})
            if path == "/api/updates" and method == "GET":
                return json_response(start_response, {"updates": updater.check()})
            if path == "/api/updates/manager" and method == "POST":
                return json_response(start_response, updater.start_manager_update(), 202)
            if path == "/api/updates/rclone" and method == "POST":
                return json_response(start_response, updater.start_rclone_update(), 202)
            if path == "/api/profile" and method == "GET":
                return json_response(start_response, {"profile": profile.get_profile()})
            if path == "/api/profile/password" and method == "POST":
                return json_response(start_response, profile.change_password(read_body_json(environ)), 202)
            if path == "/api/mounts" and method == "GET":
                return json_response(start_response, {"mounts": manager.list_mounts()})
            if path == "/api/mounts" and method == "POST":
                return json_response(start_response, {"mount": manager.save_mount(read_body_json(environ))}, 201)
            if path.startswith("/api/mounts/"):
                parts = [part for part in path.split("/") if part]
                if len(parts) < 3:
                    raise AppError("Invalid mount path", 404)
                name = parts[2]
                if len(parts) == 3 and method == "GET":
                    return json_response(start_response, {"mount": manager.get_mount(name)})
                if len(parts) == 3 and method == "PUT":
                    return json_response(start_response, {"mount": manager.save_mount(read_body_json(environ), existing=name)})
                if len(parts) == 3 and method == "DELETE":
                    manager.delete_mount(name)
                    return json_response(start_response, {"status": "deleted"})
                if len(parts) == 4 and parts[3] in {"start", "stop", "restart"} and method == "POST":
                    return json_response(start_response, {"mount": manager.control_mount(name, parts[3])})
            if path == "/api/remotes" and method == "GET":
                return json_response(start_response, {"remotes": manager.list_remotes()})
            if path == "/api/remotes" and method == "POST":
                return json_response(start_response, {"remote": manager.save_remote(read_body_json(environ))}, 201)
            if path.startswith("/api/remotes/"):
                parts = [part for part in path.split("/") if part]
                if len(parts) == 3 and parts[2] == "test" and method == "POST":
                    return json_response(start_response, {"test": manager.test_remote(read_body_json(environ))})
                if len(parts) != 3:
                    raise AppError("Invalid remote path", 404)
                name = parts[2]
                if method == "GET":
                    return json_response(start_response, {"remote": manager.get_remote(name)})
                if method == "PUT":
                    return json_response(start_response, {"remote": manager.save_remote(read_body_json(environ), existing=name)})
                if method == "DELETE":
                    manager.delete_remote(name)
                    return json_response(start_response, {"status": "deleted"})
            raise AppError(f"No route for {method} {path}", 404)
        except AppError as exc:
            return json_response(start_response, {"error": str(exc)}, exc.status)
        except Exception as exc:  # pragma: no cover
            return json_response(start_response, {"error": f"Unhandled server error: {exc}"}, 500)

    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-all", action="store_true")
    parser.add_argument("--stop-all", action="store_true")
    args = parser.parse_args()

    manager = build_manager()
    if args.start_all:
        for name in manager.start_all_mounts():
            print(name)
        return
    if args.stop_all:
        for name in manager.stop_all_mounts():
            print(name)
        return

    with make_server("0.0.0.0", DEFAULT_PORT, create_app(), server_class=ThreadedWSGIServer) as httpd:
        print(f"RCLONE MANAGER listening on :{DEFAULT_PORT} ({PLATFORM})")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
