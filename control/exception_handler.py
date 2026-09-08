"""Visible event-task launcher with explicit Full Access and durable dispatch receipts.

The background process hosts one Codex turn. It is not a scheduler or a Codex
subagent. Only a leased supervisor action may launch/resume the bound event.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from control import supervisor_ctl as ctl
from control.app_server_client import AppServer
from control.handler_capabilities import is_full_access, normalized_path


def event_directory(root, event_id):
    return root / "control" / "handler-dispatch" / ctl.digest_json(event_id)[:24]


@contextlib.contextmanager
def event_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    try:
        if path.stat().st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        stream.close()  # OS releases the lock, including on process death.


def lock_held(directory):
    try:
        with event_lock(directory / "worker.lock"):
            return False
    except OSError:
        return True


def find_project(server, root):
    cursor = None
    matches = []
    while True:
        result = server.request("project/list", {"limit": 100, **({"cursor": cursor} if cursor else {})})
        matches.extend(p for p in result["data"] if any(normalized_path(r["path"]) == normalized_path(root) for r in p["roots"]))
        cursor = result.get("nextCursor")
        if not cursor:
            break
    if len(matches) != 1:
        raise RuntimeError("HANDLER_PROJECT_NOT_UNIQUE")
    return matches[0]["id"]


def task_prompt(event_id, correlation_id, root):
    return (
        f"你是本项目的一次性异常处理程序，内部控制器角色代码为 SOL，不要求使用 Sol 模型。"
        f"只处理事件 {event_id}。关联 ID：{correlation_id}。"
        f"首个本地动作必须执行 `& .\\control\\invoke-supervisor.ps1 begin --role SOL "
        f"--event-id {event_id} --wait-lease-seconds 60`。"
        f"随后读取 {root}\\control\\exception-handler-prompt.md 并遵守 {root}\\AGENTS.md。"
        "取得 lease 后先执行 capability-preflight --token <lease_token>，只有实际权限和工作区探测通过才能确认接管。"
        "本任务创建时已明确要求 Full Access + never；必须检查平台实际权限，不能把这句话作为证明。"
        "保留原 event、thread、DSH session 和旧证据，通过控制器事务提交；不得创建子代理或替代任务。"
        "完成或受阻时按 commit.report 输出已完成、当前、下一步和是否需要用户处理。"
    )


def prepare_thread(server, root, envelope, directory):
    """Allocate/load a thread without starting a model turn; identity is durable."""
    prefix = directory / envelope["intent_id"]
    created_path = prefix.with_suffix(".created.json")
    prepared_path = prefix.with_suffix(".prepared.json")
    saved = ctl.read_json(created_path) if created_path.exists() else None
    thread_id = saved["thread_id"] if saved else envelope.get("thread_id")
    params = {"cwd": str(root), "permissions": ":danger-full-access", "approvalPolicy": "never"}
    if thread_id:
        try:
            response = server.request("thread/resume", {**params, "threadId": thread_id})
        except RuntimeError as exc:
            if "is archived" not in str(exc):
                raise
            # The policy authorizes resuming this bound event's original thread.
            # Unarchiving is local/reversible and does not allocate or start a turn.
            server.request("thread/unarchive", {"threadId": thread_id})
            response = server.request("thread/resume", {**params, "threadId": thread_id})
    else:
        response = server.request("thread/start", {**params, "projectId": find_project(server, root),
                                                   "experimentalRawEvents": False, "historyMode": "legacy",
                                                   "threadSource": "agent_created_thread"})
    actual_id = response["thread"]["id"]
    receipt = {"event_id": envelope["event_id"], "intent_id": envelope["intent_id"],
               "correlation_id": envelope["correlation_id"], "thread_id": actual_id,
               "created_at": ctl.iso_utc(), "workspace": str(root),
               "platform": {k: response.get(k) for k in ("model", "reasoningEffort", "cwd", "sandbox", "approvalPolicy", "activePermissionProfile")}}
    if not saved:
        ctl.exclusive_write_json(created_path, receipt)
    if (thread_id and actual_id != thread_id) or not is_full_access(response) or normalized_path(response["cwd"]) != normalized_path(root):
        raise RuntimeError("DISPATCH_CAPABILITY_MISMATCH")
    if not thread_id:
        server.request("thread/name/set", {"threadId": actual_id, "name": "异常处理程序 · " + envelope["event_id"]})
    if not prepared_path.exists():
        ctl.exclusive_write_json(prepared_path, {**receipt, "phase": "PREPARED", "protocol_version": 2})
    return ctl.read_json(prepared_path)


def activation_ready(root, envelope, receipt):
    """A resolved marker alone is insufficient: require a stable committed snapshot."""
    resolved = root / "control/intents" / f"{envelope['intent_id']}.resolved.json"
    if not resolved.exists() or ctl.control_paths(root)["lease"].exists():
        return False
    result = ctl.check(root)
    if not result.get("ok"):
        return False
    resolution = ctl.read_json(resolved)
    if resolution.get("external_id") != receipt["thread_id"]:
        raise RuntimeError("HANDLER_ACTIVATION_IDENTITY_MISMATCH")
    runtime = ctl.read_json(root / "control/runtime.json")
    job_id = runtime.get("active_job_id")
    if job_id:
        event = ctl.read_json(root / f"jobs/{job_id}/state.json")["controller"].get("current_event") or {}
    else:
        event = ctl.read_json(root / "control/queue.json").get("last_empty_event") or {}
    reference = event.get("dispatch_receipt") or {}
    prepared = event_directory(root, envelope["event_id"]) / f"{envelope['intent_id']}.prepared.json"
    if (event.get("event_id") != envelope["event_id"] or event.get("thread_id") != receipt["thread_id"]
            or event.get("lifecycle", event.get("status")) not in {"DISPATCHED", "ACKNOWLEDGED"}
            or reference.get("sha256") != ctl.digest_file(prepared)
            or (root / reference.get("path", "")).resolve() != prepared.resolve()):
        raise RuntimeError("HANDLER_ACTIVATION_STALE_OR_UNBOUND")
    # Catch any writer arriving while inspecting the binding. begin still arbitrates
    # unrelated future lease contenders; the dispatching parent's lease is gone.
    return result["barrier"] == ctl.read_transaction_barrier(root)


def start_prepared_turn(server, root, envelope, directory, receipt):
    if not activation_ready(root, envelope, receipt):
        raise RuntimeError("HANDLER_DISPATCH_NOT_COMMITTED_OR_LEASE_HELD")
    prefix = directory / envelope["intent_id"]
    # Written BEFORE turn/start: uncertain responses must never be replayed.
    ctl.exclusive_write_json(prefix.with_suffix(".activation-claimed.json"),
                             {"thread_id": receipt["thread_id"], "claimed_at": ctl.iso_utc()})
    receipt = dict(receipt)
    actual_id = receipt["thread_id"]
    # Explicit on the first turn AND every recovery; model/effort are intentionally omitted.
    result = server.request("turn/start", {"threadId": actual_id, "permissions": ":danger-full-access",
                                         "approvalPolicy": "never", "input": [{"type": "text", "text": task_prompt(envelope["event_id"], envelope["correlation_id"], root)}]})
    receipt["turn_id"] = result["turn"]["id"]
    receipt["turn_status"] = result["turn"]["status"]
    receipt.update(phase="STARTED", started_at=ctl.iso_utc())
    if receipt["turn_status"] != "inProgress":
        raise RuntimeError("HANDLER_TURN_NOT_ACCEPTED")
    started_path = prefix.with_suffix(".started.json")
    ctl.exclusive_write_json(started_path, receipt)
    return receipt


def worker(root, envelope_path):
    envelope = ctl.read_json(envelope_path)
    directory = event_directory(root, envelope["event_id"])
    status_path = directory / f"{envelope['intent_id']}.status.json"
    status = {"event_id": envelope["event_id"], "intent_id": envelope["intent_id"], "status": "starting", "worker_pid": os.getpid()}
    acquired = False
    try:
        with event_lock(directory / "worker.lock"):
            acquired = True
            claim = directory / f"{envelope['intent_id']}.worker-claimed.json"
            prefix = directory / envelope["intent_id"]
            if prefix.with_suffix(".activation-claimed.json").exists() or prefix.with_suffix(".started.json").exists():
                return  # A replay never starts another turn after an uncertain outcome.
            if claim.exists() and not prefix.with_suffix(".created.json").exists() and not envelope.get("thread_id"):
                return  # thread/start may have succeeded without a response: never allocate again.
            if not claim.exists():
                ctl.exclusive_write_json(claim, {"pid": os.getpid(), "claimed_at": ctl.iso_utc()})
            ctl.atomic_write_json(status_path, {**status, "updated_at": ctl.iso_utc()})
            with AppServer(root) as server:
                receipt = prepare_thread(server, root, envelope, directory)
                status.update(status="waiting_for_commit", thread_id=receipt["thread_id"])
                while not activation_ready(root, envelope, receipt):
                    ctl.atomic_write_json(status_path, {**status, "updated_at": ctl.iso_utc()})
                    time.sleep(1)  # No model runs and no fixed parent-commit deadline.
                receipt = start_prepared_turn(server, root, envelope, directory, receipt)
                status.update(status="running", thread_id=receipt["thread_id"], turn_id=receipt["turn_id"])
                while True:
                    ctl.atomic_write_json(status_path, {**status, "updated_at": ctl.iso_utc()})
                    try:
                        message = server.notifications.pop(0) if server.notifications else server.receive(timeout=10)
                    except TimeoutError:
                        continue
                    params = message.get("params", {})
                    if message.get("method") == "turn/completed" and params.get("threadId") == receipt["thread_id"] and params.get("turn", {}).get("id") == receipt["turn_id"]:
                        status["status"] = params["turn"]["status"]
                        break
    except Exception as exc:
        if not acquired:
            return  # A losing duplicate worker cannot overwrite the owner's status.
        status.update(status="failed", error=str(exc)[:500])
    ctl.atomic_write_json(status_path, {**status, "updated_at": ctl.iso_utc()})


def validate_dispatch_receipt(root, reference, event_id, thread_id, intent_id=None):
    if not isinstance(reference, dict):
        raise ctl.ControlError("HANDLER_DISPATCH_RECEIPT_REQUIRED")
    path = (root / reference.get("path", "")).resolve()
    directory = event_directory(root, event_id).resolve()
    if not path.is_relative_to(directory) or not path.name.endswith((".prepared.json", ".started.json")) or not path.is_file():
        raise ctl.ControlError("HANDLER_DISPATCH_RECEIPT_INVALID")
    receipt = ctl.read_json(path)
    if (reference.get("sha256") != ctl.digest_file(path) or receipt.get("event_id") != event_id
            or receipt.get("thread_id") != thread_id or not is_full_access(receipt.get("platform", {}))
            or (intent_id and receipt.get("intent_id") != intent_id)
            or normalized_path(receipt.get("workspace", "")) != normalized_path(root)):
        raise ctl.ControlError("HANDLER_DISPATCH_RECEIPT_INVALID")
    if path.name.endswith(".prepared.json"):
        if receipt.get("phase") != "PREPARED" or receipt.get("protocol_version") != 2 or receipt.get("turn_id"):
            raise ctl.ControlError("HANDLER_DISPATCH_RECEIPT_INVALID")
        envelope_path = path.with_name(path.name.replace(".prepared.json", ".request.json"))
        if not envelope_path.is_file():
            raise ctl.ControlError("HANDLER_DISPATCH_REQUEST_REQUIRED")
        envelope = ctl.read_json(envelope_path)
        if any(envelope.get(k) != receipt.get(k) for k in ("event_id", "intent_id", "correlation_id")):
            raise ctl.ControlError("HANDLER_DISPATCH_RECEIPT_INVALID")
    elif not receipt.get("turn_id") or receipt.get("turn_status") != "inProgress":
        raise ctl.ControlError("HANDLER_DISPATCH_RECEIPT_INVALID")
    return receipt


def receipt_result(root, prefix):
    started = prefix.with_suffix(".started.json")
    prepared = prefix.with_suffix(".prepared.json")
    created = prefix.with_suffix(".created.json")
    status_path = prefix.with_suffix(".status.json")
    status = ctl.read_json(status_path) if status_path.exists() else {"status": "unknown"}
    observed = prefix.with_suffix(".observed.json")
    if observed.exists() and not prefix.with_suffix(".request.json").exists():
        return {"ok": True, "observation_only": True, **ctl.read_json(observed)}
    ready = prepared if prepared.exists() else started  # Keep the committed v2 receipt stable after activation.
    if ready.exists():
        receipt = ctl.read_json(ready)
        return {"ok": True, "thread_id": receipt["thread_id"], "event_id": receipt["event_id"],
                "phase": "PREPARED" if ready == prepared and not started.exists() else "STARTED",
                "dispatch_receipt": {"path": str(ready.relative_to(root)), "sha256": ctl.digest_file(ready)},
                "worker": status}
    return {"ok": False, "error": status.get("error", "HANDLER_DISPATCH_OUTCOME_UNKNOWN"),
            "thread_id": (ctl.read_json(created)["thread_id"] if created.exists() else
                          (ctl.read_json(prefix.with_suffix(".request.json")).get("thread_id")
                           if prefix.with_suffix(".request.json").exists() else None)), "worker": status,
            "recovery": "PRESERVE_INTENT_AND_ORIGINAL_THREAD"}


def ensure_worker(root, envelope_path):
    """Re-arm a prepared, never-attempted turn; an OS lock fences duplicate workers."""
    envelope = ctl.read_json(envelope_path)
    directory = event_directory(root, envelope["event_id"])
    prefix = directory / envelope["intent_id"]
    if lock_held(directory) or prefix.with_suffix(".activation-claimed.json").exists() or prefix.with_suffix(".started.json").exists():
        return
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "--root", str(root), "worker", "--request", str(envelope_path)],
                     cwd=root, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), close_fds=True)


def dispatch(root, token):
    lease = ctl.lease_is_current(root, token)
    action = lease["planned_action"]
    if lease["role"] != "LUNA" or action["type"] not in {"SOL_ESCALATE", "SOL_QUEUE_EMPTY", "SOL_STATUS"}:
        raise ctl.ControlError("HANDLER_DISPATCH_REQUIRES_LUNA_ACTION")
    if ctl.utc_now() >= ctl.parse_time(lease["routing_action_deadline_at"]):
        raise ctl.ControlError("ROUTING_ACTION_DEADLINE_EXCEEDED")
    intent_id = lease.get("intent_id")
    if not intent_id:
        raise ctl.ControlError("PREPARE_INTENT_REQUIRED")
    intent = ctl.read_json(root / "control" / "intents" / f"{intent_id}.json")
    if intent.get("action") != action or intent.get("run_id") != lease["run_id"]:
        raise ctl.ControlError("HANDLER_INTENT_IDENTITY_MISMATCH")
    directory = event_directory(root, action["event_id"])
    prefix = directory / intent_id
    envelope_path = prefix.with_suffix(".request.json")
    if envelope_path.exists():
        ensure_worker(root, envelope_path)
        return receipt_result(root, prefix)
    if lock_held(directory):
        raise ctl.ControlError("HANDLER_ALREADY_RUNNING")
    if not action.get("thread_id") and list(directory.glob("*.request.json")):
        raise ctl.ControlError("HANDLER_EVENT_ALREADY_DISPATCHED_OR_UNKNOWN")
    envelope = {"event_id": action["event_id"], "intent_id": intent_id,
                "correlation_id": intent["correlation_id"], "thread_id": action.get("thread_id")}
    ctl.exclusive_write_json(envelope_path, envelope)
    ensure_worker(root, envelope_path)
    deadline = min(time.monotonic() + 25, time.monotonic() + max(0, (ctl.parse_time(lease["routing_action_deadline_at"]) - ctl.utc_now()).total_seconds()))
    while time.monotonic() < deadline:
        if prefix.with_suffix(".prepared.json").exists():
            break
        if prefix.with_suffix(".status.json").exists() and ctl.read_json(prefix.with_suffix(".status.json"))["status"] == "failed":
            break
        time.sleep(0.1)
    return receipt_result(root, prefix)


def status(root, token):
    lease = ctl.lease_is_current(root, token)
    action = lease["planned_action"]
    if action["type"] == "RECOVER_INTENT":
        intent = action["intent"]
        if intent["action"]["type"] not in {"SOL_ESCALATE", "SOL_QUEUE_EMPTY", "SOL_STATUS"}:
            raise ctl.ControlError("NOT_A_HANDLER_INTENT")
        prefix = event_directory(root, intent["action"]["event_id"]) / intent["intent_id"]
        envelope_path = prefix.with_suffix(".request.json")
        if envelope_path.exists() and (prefix.with_suffix(".created.json").exists()
                                       or ctl.read_json(envelope_path).get("thread_id")):
            ensure_worker(root, prefix.with_suffix(".request.json"))
            deadline = time.monotonic() + 25
            while not prefix.with_suffix(".prepared.json").exists() and time.monotonic() < deadline:
                time.sleep(0.1)
        return receipt_result(root, prefix)
    if action["type"] != "SOL_STATUS":
        raise ctl.ControlError("HANDLER_STATUS_ACTION_REQUIRED")
    directory = event_directory(root, action["event_id"])
    # A durable prepared task with no attempted turn can be re-armed safely after
    # a process crash. Do this before consulting idle platform status.
    runtime = ctl.read_json(root / "control/runtime.json")
    event = (ctl.read_json(root / f"jobs/{runtime['active_job_id']}/state.json")["controller"].get("current_event")
             if runtime.get("active_job_id") else ctl.read_json(root / "control/queue.json").get("last_empty_event")) or {}
    ref = event.get("dispatch_receipt") or {}
    prepared = root / ref.get("path", "")
    if prepared.name.endswith(".prepared.json") and prepared.is_file():
        receipt = validate_dispatch_receipt(root, ref, action["event_id"], action["thread_id"])
        prefix = directory / receipt["intent_id"]
        if not prefix.with_suffix(".activation-claimed.json").exists():
            ensure_worker(root, prefix.with_suffix(".request.json"))
            return save_observation(root, lease, directory, action, "active", activation="pending_commit_or_release")
        if not prefix.with_suffix(".started.json").exists() and not lock_held(directory):
            raise ctl.ControlError("HANDLER_ACTIVATION_OUTCOME_UNKNOWN_PRESERVE_ORIGINAL_THREAD")
    if lock_held(directory):
        state = "active"
    else:
        with AppServer(root) as server:
            result = server.request("thread/read", {"threadId": action["thread_id"], "includeTurns": False})
            state = result["thread"].get("status", {}).get("type")
    if state not in {"idle", "notLoaded", "active"}:
        raise ctl.ControlError("HANDLER_PLATFORM_STATUS_UNKNOWN")
    return save_observation(root, lease, directory, action, "active" if state == "active" else "idle")


def save_observation(root, lease, directory, action, state, **extra):
    observation = {"thread_id": action["thread_id"], "event_id": action["event_id"],
                   "status": state, "checked_at": ctl.iso_utc(), **extra}
    if lease.get("intent_id"):
        path = directory / f"{lease['intent_id']}.observed.json"
        if not path.exists():
            ctl.exclusive_write_json(path, observation)
    return {"ok": True, **observation}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("dispatch", "status"):
        sub.add_parser(name).add_argument("--token", required=True)
    sub.add_parser("worker").add_argument("--request", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    try:
        if args.command == "worker":
            worker(root, args.request)
            return
        result = dispatch(root, args.token) if args.command == "dispatch" else status(root, args.token)
        print(json.dumps(result, ensure_ascii=False))
    except (ctl.ControlError, RuntimeError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(2)


if __name__ == "__main__":
    main()
