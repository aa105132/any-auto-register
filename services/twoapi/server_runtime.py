from __future__ import annotations

import os
import platform
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT / "output"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 6543


class TwoAPIServerRuntime:
    """管理独立 2API 本地代理服务。"""

    def __init__(
        self,
        *,
        root: Path | None = None,
        data_dir: Path | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        wait_interval: float = 0.2,
    ) -> None:
        self.root = Path(root or ROOT)
        self.data_dir = Path(data_dir or (self.root / "output"))
        self.host = host
        self.port = int(port)
        self.wait_interval = max(0.0, float(wait_interval))
        self._process: subprocess.Popen[Any] | None = None
        self._lock = threading.Lock()

    @property
    def listen(self) -> str:
        return f"http://{self.host}:{self.port}/zo/v1"

    @property
    def log_path(self) -> Path:
        return self.data_dir / "twoapi_server.log"

    @property
    def err_log_path(self) -> Path:
        return self.data_dir / "twoapi_server.err.log"

    def _is_port_open(self, timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return True
        except OSError:
            return False

    def _owned_process_alive(self) -> bool:
        return bool(self._process and self._process.poll() is None)

    def status(self) -> dict[str, Any]:
        running = self._is_port_open()
        pid = self._process.pid if self._owned_process_alive() else None
        return {
            "ok": running,
            "running": running,
            "started": False,
            "owned": self._owned_process_alive(),
            "pid": pid,
            "host": self.host,
            "port": self.port,
            "listen": self.listen,
            "log_path": str(self.log_path),
            "err_log_path": str(self.err_log_path),
        }

    def ensure_running(self, *, timeout_seconds: float = 10.0) -> dict[str, Any]:
        with self._lock:
            if self._is_port_open():
                return {
                    "ok": True,
                    "running": True,
                    "started": False,
                    "owned": self._owned_process_alive(),
                    "pid": self._process.pid if self._owned_process_alive() and self._process else None,
                    "host": self.host,
                    "port": self.port,
                    "listen": self.listen,
                    "log_path": str(self.log_path),
                    "err_log_path": str(self.err_log_path),
                }

            script = self.root / "scripts" / "run_twoapi_server.py"
            if not script.exists():
                result = self.status()
                result.update({"ok": False, "running": False, "started": False, "error": f"2API 启动脚本不存在: {script}"})
                return result

            self.data_dir.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["PYTHONUTF8"] = "1"
            env["PYTHONIOENCODING"] = "utf-8"
            env["TWOAPI_CHILD_SERVER"] = "1"
            creationflags = 0x08000000 if platform.system() == "Windows" else 0

            try:
                with self.log_path.open("ab") as stdout_file, self.err_log_path.open("ab") as stderr_file:
                    self._process = subprocess.Popen(
                        [sys.executable, str(script)],
                        cwd=str(self.root),
                        stdin=subprocess.DEVNULL,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        env=env,
                        creationflags=creationflags,
                    )
            except Exception as exc:
                result = self.status()
                result.update({"ok": False, "running": False, "started": False, "error": str(exc)})
                return result

            deadline = time.monotonic() + max(0.0, float(timeout_seconds))
            while True:
                if self._is_port_open():
                    return {
                        "ok": True,
                        "running": True,
                        "started": True,
                        "owned": self._owned_process_alive(),
                        "pid": self._process.pid if self._process else None,
                        "host": self.host,
                        "port": self.port,
                        "listen": self.listen,
                        "log_path": str(self.log_path),
                        "err_log_path": str(self.err_log_path),
                    }
                if self._process and self._process.poll() is not None:
                    break
                if time.monotonic() >= deadline:
                    break
                time.sleep(self.wait_interval)

            result = self.status()
            exit_code = self._process.poll() if self._process else None
            error = "2API 服务启动超时"
            if exit_code is not None:
                error = f"2API 服务已退出，退出码 {exit_code}"
            result.update({"ok": False, "running": False, "started": True, "pid": self._process.pid if self._process else None, "error": error})
            return result

    def stop_owned(self, *, timeout_seconds: float = 5.0) -> dict[str, Any]:
        with self._lock:
            if not self._owned_process_alive():
                result = self.status()
                result.update({"stopped": False, "reason": "not_owned_or_not_running"})
                return result

            process = self._process
            assert process is not None
            process.terminate()
            try:
                process.wait(timeout=max(0.1, float(timeout_seconds)))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2.0)
            result = self.status()
            result.update({"stopped": True})
            return result


twoapi_server_runtime = TwoAPIServerRuntime()
