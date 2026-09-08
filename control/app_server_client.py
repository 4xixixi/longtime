"""Small stdio client for the installed Codex App Server (no API credentials)."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import time


def codex_executable() -> str:
    # Prefer the Desktop-managed runtime, keeping the adapter on its protocol.
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "OpenAI/Codex/bin"
    choices = list(base.glob("*/codex.exe"))
    if choices:
        return str(max(choices, key=lambda p: p.stat().st_mtime))
    found = shutil.which("codex.exe") or shutil.which("codex")
    if not found:
        raise RuntimeError("CODEX_RUNTIME_NOT_FOUND")
    return found


class AppServer:
    def __init__(self, cwd: Path):
        self.process = subprocess.Popen(
            [codex_executable(), "app-server", "--stdio"], cwd=cwd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.messages = queue.Queue()
        self.notifications = []
        self.sequence = 0
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "longtime_exception_handler", "version": "1.0"},
                                    "capabilities": {"experimentalApi": True}})
        self.send({"method": "initialized"})

    def _read(self):
        try:
            for line in self.process.stdout:
                try:
                    self.messages.put(json.loads(line))
                except ValueError:
                    continue
        finally:
            self.messages.put({"_eof": True})

    def send(self, message):
        self.process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def receive(self, timeout=30):
        try:
            message = self.messages.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("APP_SERVER_RESPONSE_TIMEOUT") from None
        if message.get("_eof"):
            raise RuntimeError("APP_SERVER_EXITED")
        # Unattended execution must not silently approve a platform request.
        if "method" in message and "id" in message:
            self.send({"id": message["id"], "error": {"code": -32601,
                       "message": "Unexpected approval/input request in Full Access handler"}})
            raise RuntimeError("DISPATCH_CAPABILITY_MISMATCH: unexpected server request " + message["method"])
        return message

    def request(self, method, params, timeout=30):
        self.sequence += 1
        ident = self.sequence
        self.send({"id": ident, "method": method, "params": params})
        deadline = time.monotonic() + timeout
        while True:
            message = self.receive(max(0.01, deadline - time.monotonic()))
            if message.get("id") == ident:
                if "error" in message:
                    # Do not echo arbitrary server payloads, which may contain environment details.
                    raise RuntimeError(f"APP_SERVER_REQUEST_FAILED: {method}: {message['error'].get('message', '')[:500]}")
                return message["result"]
            self.notifications.append(message)
            if time.monotonic() >= deadline:
                raise TimeoutError("APP_SERVER_RESPONSE_TIMEOUT")

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=5)
        self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
