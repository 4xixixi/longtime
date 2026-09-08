"""Evidence-based Full Access gate. No caller-supplied permission assertions."""
from __future__ import annotations

import json
from contextlib import closing
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import tempfile


def normalized_path(value):
    return str(value).removeprefix("\\\\?\\").replace("\\", "/").rstrip("/").casefold()


def is_full_access(snapshot):
    policy = snapshot.get("sandbox", snapshot.get("sandbox_policy", {})) or {}
    kind = policy.get("type", "")
    approval = snapshot.get("approvalPolicy", snapshot.get("approval_policy"))
    # 'disabled' in the SQLite projection alone is not proof of a turn's permissions.
    return approval == "never" and kind in {"dangerFullAccess", "danger-full-access"}


def canonical_path(value):
    """Resolve links, then normalize Windows extended paths for containment checks."""
    resolved = str(Path(value).resolve())
    if resolved.startswith("\\\\?\\UNC\\"):
        resolved = "\\\\" + resolved[8:]
    else:
        resolved = resolved.removeprefix("\\\\?\\")
    return Path(resolved)


def platform_snapshot(thread_id):
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    with closing(sqlite3.connect((home / "state_5.sqlite").as_uri() + "?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT rollout_path FROM threads WHERE id=?", (thread_id,)).fetchone()
    if not row:
        raise RuntimeError("PLATFORM_THREAD_NOT_FOUND")
    path = canonical_path(row[0])
    if not any(path.is_relative_to(canonical_path(home / folder)) for folder in ("sessions", "archived_sessions")):
        raise RuntimeError("PLATFORM_ROLLOUT_PATH_INVALID")
    latest = None
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                record = json.loads(line)
            except ValueError:
                continue  # A live append may expose an incomplete trailing line.
            if record.get("type") == "turn_context":
                payload = record["payload"]
                latest = {k: payload.get(k) for k in ("cwd", "approval_policy", "sandbox_policy", "model", "turn_id")}
                latest["recorded_at"] = record.get("timestamp")
    if latest is None:
        raise RuntimeError("PLATFORM_TURN_PERMISSION_EVIDENCE_UNAVAILABLE")
    return latest


def probe_workspace(workspace: str):
    """Write/read/remove only a newly allocated scratch directory, with bounded WSL execution."""
    match = re.fullmatch(r"\\\\(?:wsl\.localhost|wsl\$)\\([^\\]+)\\(.+)", workspace, re.I)
    if match:
        distro, tail = match.groups()
        path = "/" + tail.replace("\\", "/")
        code = (
            "import pathlib,tempfile; p=pathlib.Path(__import__('sys').argv[1]); "
            "assert p.is_dir(); "
            "d=tempfile.TemporaryDirectory(prefix='.longtime-capability-',dir=p); "
            "f=pathlib.Path(d.name)/'probe'; f.write_text('capability'); "
            "assert f.read_text()=='capability'; d.cleanup(); print('WSL_READ_WRITE_OK')"
        )
        completed = subprocess.run(["wsl.exe", "-d", distro, "--", "python3", "-c", code, path],
                                   capture_output=True, text=True, encoding="utf-8", timeout=20,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode != 0 or completed.stdout.strip() != "WSL_READ_WRITE_OK":
            raise RuntimeError(f"WSL_READ_WRITE_FAILED: exit={completed.returncode}")
        return {"filesystem_read_write": True, "shell": True, "wsl": True, "scratch_removed": True}
    with tempfile.TemporaryDirectory(prefix=".longtime-capability-", dir=workspace) as directory:
        p = Path(directory) / "probe"
        p.write_text("capability", encoding="utf-8")
        if p.read_text(encoding="utf-8") != "capability":
            raise RuntimeError("WORKSPACE_READ_WRITE_FAILED")
    executable = "powershell.exe" if os.name == "nt" else "/bin/sh"
    command = [executable, "-NoProfile", "-NonInteractive", "-Command", "exit 0"] if os.name == "nt" else [executable, "-c", "exit 0"]
    subprocess.run(command, check=True, timeout=10, capture_output=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return {"filesystem_read_write": True, "shell": True, "wsl": None, "scratch_removed": True}


def preflight(root, lease):
    from control import supervisor_ctl as ctl
    action = lease["planned_action"]
    if lease["role"] != "SOL" or action["type"] not in {"SOL_HANDLE_EVENT", "SOL_HANDLE_QUEUE_EMPTY"}:
        raise ctl.ControlError("CAPABILITY_PREFLIGHT_REQUIRES_HANDLER_LEASE")
    receipt_path = root / "control" / "capabilities" / f"{lease['run_id']}.json"
    if receipt_path.exists():
        record = ctl.read_json(receipt_path)
        return {"ok": record["ok"], "capability_receipt": str(receipt_path.relative_to(root)),
                "sha256": ctl.digest_file(receipt_path), "error": record.get("error")}
    workspace = action.get("workspace") or str(root)
    record = {"run_id": lease["run_id"], "event_id": action["event_id"],
              "thread_id": lease["source_thread_id"], "workspace": workspace,
              "checked_at": ctl.iso_utc(), "required": "FULL_ACCESS_NO_APPROVAL", "ok": False}
    try:
        snapshot = platform_snapshot(lease["source_thread_id"])
        record["platform"] = snapshot
        if not is_full_access(snapshot) or normalized_path(snapshot["cwd"]) != normalized_path(root):
            raise RuntimeError("DISPATCH_CAPABILITY_MISMATCH")
        record["control_workspace"] = probe_workspace(str(root))
        record["workspace_probe"] = probe_workspace(workspace)
        record["ok"] = True
    except (RuntimeError, OSError, subprocess.SubprocessError, sqlite3.Error) as exc:
        record["error"] = str(exc)[:500]
    ctl.exclusive_write_json(receipt_path, record)
    return {"ok": record["ok"], "capability_receipt": str(receipt_path.relative_to(root)),
            "sha256": ctl.digest_file(receipt_path), "error": record.get("error")}


def validate_preflight(root, lease, passed=True):
    from control import supervisor_ctl as ctl
    path = root / "control" / "capabilities" / f"{lease['run_id']}.json"
    if not path.is_file():
        raise ctl.ControlError("HANDLER_CAPABILITY_PREFLIGHT_REQUIRED")
    record = ctl.read_json(path)
    action = lease["planned_action"]
    if (record.get("run_id") != lease["run_id"] or record.get("event_id") != action.get("event_id")
            or record.get("thread_id") != lease.get("source_thread_id")
            or record.get("workspace") != (action.get("workspace") or str(root))
            or record.get("ok") is not passed):
        raise ctl.ControlError("HANDLER_CAPABILITY_RECEIPT_INVALID")
    if passed:
        if (not is_full_access(record.get("platform", {}))
                or not all(record.get("workspace_probe", {}).get(k) is True for k in ("filesystem_read_write", "shell", "scratch_removed"))
                or not all(record.get("control_workspace", {}).get(k) is True for k in ("filesystem_read_write", "shell", "scratch_removed"))):
            raise ctl.ControlError("HANDLER_CAPABILITY_RECEIPT_INVALID")
        # Recheck actual permissions at commit; changing a manifest cannot confer access.
        try:
            current = platform_snapshot(lease["source_thread_id"])
        except (RuntimeError, OSError, sqlite3.Error) as exc:
            raise ctl.ControlError("PLATFORM_PERMISSION_EVIDENCE_UNAVAILABLE") from exc
        if not is_full_access(current) or normalized_path(current.get("cwd", "")) != normalized_path(root):
            raise ctl.ControlError("DISPATCH_CAPABILITY_MISMATCH")
    return {"path": str(path.relative_to(root)), "sha256": ctl.digest_file(path)}
