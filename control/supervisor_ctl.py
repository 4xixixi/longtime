#!/usr/bin/env python3
"""Deterministic safety shell for the Longtime supervisor.

The LLM decides how to interpret evidence.  This module owns the parts that
must be repeatable: one-writer leases, state validation, action planning,
write-ahead intents, and recoverable multi-file commits.
"""

from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.modules.setdefault("control.supervisor_ctl", sys.modules[__name__])


RUNTIME_SCHEMA = 7
QUEUE_SCHEMA = 4
JOB_SCHEMA = 5
WORKFLOW_VERSION = "8.1"

OPEN_EVENT_LIFECYCLES = {"REQUIRED", "DISPATCHED", "ACKNOWLEDGED"}
EVENT_LIFECYCLES = OPEN_EVENT_LIFECYCLES | {"RESOLVED", "SUPERSEDED"}
OPEN_USER_GATE_LIFECYCLES = {"REQUIRED"}
TERMINAL_DSH_LIFECYCLES = {"completed", "cancelled", "failed", "lost"}
ACTIVE_DSH_LIFECYCLES = {"starting", "running"}

VALID_JOB_STATUSES = {
    "DRAFT",
    "QUEUED",
    "RUNNING",
    "REVIEW_PENDING",
    "BLOCKED",
    "FAILED",
    "CANCELLED",
    "ACCEPTED",
    "ARCHIVED",
}
VALID_USER_GATES = {
    "DESTRUCTIVE_DATA_LOSS",
    "EXTERNAL_REMOTE_EFFECT",
    "PRIVILEGE_SECRET_SYSTEM",
    "PAID_BUDGET_INCREASE",
    "PROJECT_OUTCOME_PHASE",
    "LEGAL_SAFETY_HUMAN",
}
VALID_TRANSITIONS = {
    "DRAFT": {"DRAFT", "QUEUED", "BLOCKED", "CANCELLED"},
    "QUEUED": {"QUEUED", "RUNNING", "BLOCKED", "FAILED", "CANCELLED"},
    "RUNNING": {"RUNNING", "REVIEW_PENDING", "BLOCKED", "FAILED", "CANCELLED"},
    "REVIEW_PENDING": {"REVIEW_PENDING", "QUEUED", "BLOCKED", "FAILED", "ACCEPTED"},
    "BLOCKED": {"BLOCKED", "QUEUED", "RUNNING", "REVIEW_PENDING", "FAILED", "CANCELLED"},
    "FAILED": {"FAILED", "QUEUED", "RUNNING", "BLOCKED", "CANCELLED"},
    "CANCELLED": {"CANCELLED"},
    "ACCEPTED": {"ACCEPTED", "ARCHIVED"},
    "ARCHIVED": {"ARCHIVED"},
}
EXTERNAL_ACTIONS = {
    "DSH_START",
    "DSH_CONTINUE",
    "DSH_STATUS",
    "SOL_ESCALATE",
    "SOL_QUEUE_EMPTY",
    "SOL_STATUS",
    "RECOVER_INTENT",
}
ROUTING_ACTIONS = EXTERNAL_ACTIONS | {"ACTIVATE_JOB"}
ROLE_VALUES = {"LUNA", "SOL", "MIGRATION", "USER"}


class ControlError(RuntimeError):
    """A deterministic control-plane validation failure."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_utc(value: datetime | None = None) -> str:
    value = value or utc_now()
    return value.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_bytes(value))


def persisted_json_bytes(value: Any) -> bytes:
    """Return the exact byte representation used by atomic_write_json."""
    return json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"


def digest_persisted_json(value: Any) -> str:
    return digest_bytes(persisted_json_bytes(value))


def digest_file(path: Path) -> str:
    return digest_bytes(path.read_bytes())


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise ControlError(f"missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ControlError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControlError(f"expected JSON object in {path}")
    return value


def atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_bytes(path, persisted_json_bytes(value))


def exclusive_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise ControlError(f"file already exists: {path}") from exc


def append_jsonl(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = canonical_bytes(value)
    descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        os.write(descriptor, data)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def require(mapping: dict[str, Any], keys: tuple[str, ...], label: str) -> None:
    missing = [key for key in keys if key not in mapping]
    if missing:
        raise ControlError(f"{label} missing required fields: {', '.join(missing)}")


def control_paths(root: Path) -> dict[str, Path]:
    return {
        "runtime": root / "control" / "runtime.json",
        "queue": root / "control" / "queue.json",
        "project_contract": root / "control" / "project-contract.json",
        "lease": root / "control" / "run-lease.json",
        "transactions": root / "control" / "transactions",
        "intents": root / "control" / "intents",
        "heartbeat_log": root / "control" / "heartbeat-log.jsonl",
        "stale_leases": root / "control" / "stale-leases",
    }


def relative(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def state_path(root: Path, job_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", job_id):
        raise ControlError(f"invalid job_id: {job_id!r}")
    return root / "jobs" / job_id / "state.json"


def protocol_contract_path(root: Path, job_id: str) -> Path:
    return root / "jobs" / job_id / "protocol-contract.json"


def specification_path(root: Path, job_id: str) -> Path:
    return root / "jobs" / job_id / "specification.md"


def staged_protocol_file_updates(
    root: Path,
    lease: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    staging = request.get("protocol_staging")
    if not request.get("adopt_protocol_contract"):
        if staging is not None:
            raise ControlError("protocol_staging requires adopt_protocol_contract=true")
        return {}
    if lease.get("role") != "SOL" or lease.get("planned_action", {}).get("type") != "SOL_HANDLE_EVENT":
        raise ControlError("staged protocol adoption requires the bound Sol event")
    if not isinstance(staging, dict):
        raise ControlError("protocol adoption requires protocol_staging")
    job_id = request.get("job_id") or lease.get("planned_action", {}).get("job_id")
    if not job_id:
        raise ControlError("protocol adoption requires job_id")
    expected_names = {"specification", "protocol_contract"}
    if set(staging) != expected_names:
        raise ControlError("protocol_staging must contain specification and protocol_contract")
    staging_root = (root / "control" / "staging").resolve()
    sources: dict[str, bytes] = {}
    for key, value in staging.items():
        if not isinstance(value, str):
            raise ControlError(f"protocol_staging.{key} must be a relative path")
        source = (root / value).resolve()
        try:
            source.relative_to(staging_root)
        except ValueError as exc:
            raise ControlError(f"protocol_staging.{key} must be under control/staging") from exc
        try:
            sources[key] = source.read_bytes()
        except FileNotFoundError as exc:
            raise ControlError(f"missing staged protocol file: {value}") from exc
    targets = {
        f"jobs/{job_id}/specification.md": sources["specification"],
        f"jobs/{job_id}/protocol-contract.json": sources["protocol_contract"],
    }
    return {
        rel_path: {
            "encoding": "base64",
            "data": base64.b64encode(data).decode("ascii"),
            "sha256": digest_bytes(data),
        }
        for rel_path, data in targets.items()
    }


def collect_documents(root: Path) -> dict[str, dict[str, Any]]:
    paths = control_paths(root)
    documents = {
        relative(root, paths["runtime"]): read_json(paths["runtime"]),
        relative(root, paths["queue"]): read_json(paths["queue"]),
    }
    # Project contracts were not part of the original v8 transaction snapshot.
    # Include them for new stores and after the first explicit contract migration,
    # while remaining able to open a legacy snapshot long enough to migrate it.
    project_rel = relative(root, paths["project_contract"])
    latest = latest_committed_transaction(root)
    latest_documents = latest[1].get("documents", {}) if latest is not None else {}
    if paths["project_contract"].exists() and (latest is None or project_rel in latest_documents):
        documents[project_rel] = read_json(paths["project_contract"])
    jobs_root = root / "jobs"
    if jobs_root.exists():
        for path in sorted(jobs_root.glob("*/state.json")):
            documents[relative(root, path)] = read_json(path)
    return documents


def document_hashes(documents: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {path: digest_json(value) for path, value in sorted(documents.items())}


def file_update_hashes(file_updates: dict[str, dict[str, Any]]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for rel_path, update in sorted(file_updates.items()):
        if not isinstance(update, dict) or update.get("encoding") != "base64":
            raise ControlError(f"transaction file update {rel_path} has an invalid encoding")
        try:
            data = base64.b64decode(update.get("data", ""), validate=True)
        except Exception as exc:
            raise ControlError(f"transaction file update {rel_path} is not valid base64") from exc
        digest = digest_bytes(data)
        if update.get("sha256") != digest:
            raise ControlError(f"transaction file update {rel_path} hash mismatch")
        hashes[rel_path] = digest
    return hashes


def latest_transaction(root: Path) -> tuple[Path, dict[str, Any], bool] | None:
    """Return the newest directory containing prepare.json (legacy helper).

    Routing and drift checks must use latest_committed_transaction() instead.
    """
    transactions = control_paths(root)["transactions"]
    if not transactions.exists():
        return None
    candidates = sorted(path for path in transactions.iterdir() if path.is_dir() and (path / "prepare.json").exists())
    if not candidates:
        return None
    path = candidates[-1]
    return path, read_json(path / "prepare.json"), (path / "commit.json").exists()


def _validated_committed_transaction(path: Path) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
    prepare_path = path / "prepare.json"
    commit_path = path / "commit.json"
    if not prepare_path.exists() or not commit_path.exists():
        return None
    try:
        prepare = read_json(prepare_path)
        commit = read_json(commit_path)
    except ControlError:
        return None
    transaction_id = path.name
    if prepare.get("transaction_id") != transaction_id or commit.get("transaction_id") != transaction_id:
        return None
    documents = prepare.get("documents")
    if not isinstance(documents, dict) or not documents:
        return None
    try:
        expected = document_hashes(documents)
        expected_file_updates = file_update_hashes(prepare.get("file_updates") or {})
    except Exception:
        return None
    if commit.get("document_hashes") != expected:
        return None
    if commit.get("file_update_hashes", {}) != expected_file_updates:
        return None
    return path, prepare, commit


def latest_committed_transaction(root: Path) -> tuple[Path, dict[str, Any], dict[str, Any]] | None:
    """Select the newest transaction with a self-consistent prepare/commit pair."""
    directory = control_paths(root)["transactions"]
    if not directory.exists():
        return None
    # Most callers need only the head, not a rehash of every historical snapshot.
    # pending_transactions/recover_transactions still audit the entire journal.
    for path in sorted((item for item in directory.iterdir() if item.is_dir()), reverse=True):
        item = _validated_committed_transaction(path)
        if item is not None:
            return item
    return None


def pending_transactions(root: Path) -> list[dict[str, str]]:
    directory = control_paths(root)["transactions"]
    if not directory.exists():
        return []
    pending: list[dict[str, str]] = []
    for path in sorted(item for item in directory.iterdir() if item.is_dir()):
        prepare_path = path / "prepare.json"
        if not prepare_path.exists():
            continue
        if _validated_committed_transaction(path) is None:
            pending.append(
                {
                    "transaction_id": path.name,
                    "prepare_sha256": digest_file(prepare_path),
                    "commit_sha256": digest_file(path / "commit.json") if (path / "commit.json").exists() else "",
                }
            )
    return pending


def _lease_barrier(root: Path) -> dict[str, Any]:
    path = control_paths(root)["lease"]
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {"state": "ABSENT", "sha256": digest_bytes(b"")}
    digest = digest_bytes(data)
    try:
        lease = json.loads(data.decode("utf-8-sig"))
        state = "LIVE" if parse_time(lease["expires_at"]) > utc_now() else "EXPIRED"
    except Exception:
        state = "EXPIRED"
    return {"state": state, "sha256": digest}


def read_transaction_barrier(root: Path) -> dict[str, Any]:
    committed = latest_committed_transaction(root)
    head = (
        {
            "transaction_id": committed[0].name,
            "commit_sha256": digest_file(committed[0] / "commit.json"),
        }
        if committed
        else {"transaction_id": None, "commit_sha256": digest_bytes(b"")}
    )
    return {
        "lease": _lease_barrier(root),
        "committed_head": head,
        "pending_transactions_sha256": digest_json(pending_transactions(root)),
    }


def prepare_transaction_writes(
    root: Path, transaction_dir: Path, prepare: dict[str, Any]
) -> tuple[list[tuple[Path, bytes]], dict[str, str], dict[str, str]]:
    """Validate and serialize the whole payload before replacing any file.

    This prevents deterministic payload errors from partially publishing a
    snapshot. I/O failures still use the existing recoverable roll-forward.
    """
    if prepare.get("transaction_id") != transaction_dir.name:
        raise ControlError(f"transaction_id does not match directory: {transaction_dir.name}")
    documents = prepare.get("documents")
    if not isinstance(documents, dict) or not documents:
        raise ControlError(f"transaction {transaction_dir.name} has no documents")
    file_updates = prepare.get("file_updates", {})
    if not isinstance(file_updates, dict):
        raise ControlError("transaction file_updates must be an object")
    expected_file_updates = file_update_hashes(file_updates)
    writes: list[tuple[Path, bytes]] = []
    destinations: set[Path] = set()

    def add_write(rel_path: str, data: bytes) -> None:
        if not isinstance(rel_path, str) or not rel_path or Path(rel_path).anchor:
            raise ControlError(f"transaction destination must be a relative path: {rel_path!r}")
        destination = (root / rel_path).resolve()
        try:
            destination.relative_to(root.resolve())
        except ValueError as exc:
            raise ControlError(f"transaction path escapes root: {rel_path}") from exc
        if destination in destinations:
            raise ControlError(f"duplicate transaction destination: {rel_path}")
        if destination.is_dir():
            raise ControlError(f"transaction destination is a directory: {rel_path}")
        destinations.add(destination)
        writes.append((destination, data))

    for rel_path, update in sorted(file_updates.items()):
        add_write(rel_path, base64.b64decode(update["data"], validate=True))

    for rel_path, document in sorted(documents.items()):
        if not isinstance(document, dict):
            raise ControlError(f"transaction document {rel_path} is not an object")
        add_write(rel_path, persisted_json_bytes(document))

    resolution = prepare.get("intent_resolution")
    if resolution is not None:
        if not isinstance(resolution, dict):
            raise ControlError("transaction intent_resolution must be an object")
        intent_id = resolution.get("intent_id")
        if not isinstance(intent_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", intent_id):
            raise ControlError("transaction intent_id must be a safe nonempty identifier")
        add_write(f"control/intents/{intent_id}.resolved.json", persisted_json_bytes(resolution))

    return writes, document_hashes(documents), expected_file_updates


def roll_forward(root: Path, transaction_dir: Path, prepare: dict[str, Any]) -> None:
    writes, hashes, expected_file_updates = prepare_transaction_writes(root, transaction_dir, prepare)
    for destination, data in writes:
        atomic_write_bytes(destination, data)
    commit = {
        "schema_version": 1,
        "transaction_id": prepare["transaction_id"],
        "committed_at": iso_utc(),
        "document_hashes": hashes,
        "file_update_hashes": expected_file_updates,
    }
    atomic_write_json(transaction_dir / "commit.json", commit)


def recover_transactions(root: Path) -> list[str]:
    recovered: list[str] = []
    transactions = control_paths(root)["transactions"]
    if not transactions.exists():
        return recovered
    pending: list[tuple[Path, dict[str, Any]]] = []
    committed_head: str | None = None
    for transaction_dir in sorted(path for path in transactions.iterdir() if path.is_dir()):
        prepare_path = transaction_dir / "prepare.json"
        commit_path = transaction_dir / "commit.json"
        if commit_path.exists():
            if _validated_committed_transaction(transaction_dir) is None:
                raise ControlError(f"CORRUPT_COMMITTED_TRANSACTION: {transaction_dir.name}")
            committed_head = transaction_dir.name
        elif prepare_path.exists():
            prepare = read_json(prepare_path)
            prepare_transaction_writes(root, transaction_dir, prepare)
            pending.append((transaction_dir, prepare))
    # A committed successor proves an older uncommitted snapshot must not be
    # replayed. Preserve both records for diagnosis instead of rolling back.
    for transaction_dir, _prepare in pending:
        if committed_head is not None and transaction_dir.name < committed_head:
            raise ControlError(f"OUT_OF_ORDER_PENDING_TRANSACTION: {transaction_dir.name}")
    for transaction_dir, prepare in pending:
        roll_forward(root, transaction_dir, prepare)
        recovered.append(transaction_dir.name)
    return recovered


def verify_transaction_snapshot(root: Path, documents: dict[str, dict[str, Any]]) -> None:
    latest = latest_committed_transaction(root)
    if latest is None:
        raise ControlError("state store is not bootstrapped")
    transaction_dir, prepare, _commit = latest
    expected_documents = prepare.get("documents", {})
    expected_hashes = document_hashes(expected_documents)
    actual_hashes = document_hashes(documents)
    if expected_hashes != actual_hashes:
        differing = sorted(set(expected_hashes) | set(actual_hashes))
        differing = [path for path in differing if expected_hashes.get(path) != actual_hashes.get(path)]
        raise ControlError(f"state changed outside supervisor_ctl: {', '.join(differing)}")


def validate_runtime(runtime: dict[str, Any]) -> None:
    require(runtime, ("schema_version", "updated_at", "project_status", "active_job_id", "controller", "defaults"), "runtime")
    if runtime["schema_version"] != RUNTIME_SCHEMA:
        raise ControlError(f"runtime schema must be {RUNTIME_SCHEMA}")
    if runtime["project_status"] not in {"ACTIVE", "COMPLETED", "FAILED", "PAUSED"}:
        raise ControlError(f"invalid project_status: {runtime['project_status']}")
    controller = runtime["controller"]
    require(controller, ("workflow_version", "project_contract_sha256", "user_only_gates"), "runtime.controller")
    if controller["workflow_version"] != WORKFLOW_VERSION:
        raise ControlError(f"workflow_version must be {WORKFLOW_VERSION}")
    if set(controller["user_only_gates"]) != VALID_USER_GATES:
        raise ControlError("runtime user_only_gates differ from the system gates")
    defaults = runtime["defaults"]
    require(
        defaults,
        (
            "max_parallel_jobs",
            "check_interval_minutes",
            "lease_ttl_minutes",
            "stale_after_minutes",
            "max_auto_rework_epochs",
            "max_auto_runtime_hours",
            "max_sol_escalations_per_job",
            "max_same_root_cause_escalations",
        ),
        "runtime.defaults",
    )
    if defaults["max_parallel_jobs"] != 1:
        raise ControlError("only one active job is supported")


def validate_queue(queue: dict[str, Any]) -> None:
    require(queue, ("schema_version", "queue_revision", "updated_at", "planning_generation", "jobs"), "queue")
    if queue["schema_version"] != QUEUE_SCHEMA:
        raise ControlError(f"queue schema must be {QUEUE_SCHEMA}")
    if not isinstance(queue["queue_revision"], int) or queue["queue_revision"] < 0:
        raise ControlError("queue_revision must be a non-negative integer")
    if not isinstance(queue["planning_generation"], int) or queue["planning_generation"] < 0:
        raise ControlError("planning_generation must be a non-negative integer")
    ids: list[str] = []
    for entry in queue["jobs"]:
        if not isinstance(entry, dict) or not entry.get("job_id"):
            raise ControlError("queue contains an invalid job entry")
        ids.append(entry["job_id"])
    if len(ids) != len(set(ids)):
        raise ControlError("queue contains duplicate job IDs")


def validate_job(job: dict[str, Any]) -> None:
    require(
        job,
        (
            "schema_version",
            "job_id",
            "status",
            "created_at",
            "updated_at",
            "workspace",
            "baseline",
            "authorized_specification",
            "dsh",
            "limits",
            "controller",
            "verification",
            "decision",
            "result",
            "runtime_tracking",
        ),
        f"job {job.get('job_id', '<unknown>')}",
    )
    if job["schema_version"] != JOB_SCHEMA:
        raise ControlError(f"job {job['job_id']} schema must be {JOB_SCHEMA}")
    if job["status"] not in VALID_JOB_STATUSES:
        raise ControlError(f"job {job['job_id']} has invalid status {job['status']}")
    controller = job["controller"]
    require(
        controller,
        (
            "state_revision",
            "semantic_revision",
            "event_generation",
            "current_event",
            "protocol_contract_sha256",
            "escalation_metrics",
        ),
        "job.controller",
    )
    for field in ("state_revision", "semantic_revision", "event_generation"):
        if not isinstance(controller[field], int) or controller[field] < 0:
            raise ControlError(f"job.controller.{field} must be a non-negative integer")
    event = controller.get("current_event")
    if event is not None:
        require(
            event,
            (
                "event_id",
                "generation",
                "origin_status",
                "origin_semantic_revision",
                "event_key",
                "root_cause_signature",
                "lifecycle",
            ),
            "job.controller.current_event",
        )
        expected_event = f"{job['job_id']}:{event['generation']}:{event['origin_status']}"
        if event["event_id"] != expected_event:
            raise ControlError(f"job technical event_id mismatch: expected {expected_event}")
        if event["generation"] > controller["event_generation"]:
            raise ControlError("technical event generation exceeds controller generation")
        if event["origin_status"] not in {"BLOCKED", "FAILED"}:
            raise ControlError("technical event origin_status must be BLOCKED or FAILED")
        if event["lifecycle"] not in EVENT_LIFECYCLES:
            raise ControlError("technical event has invalid lifecycle")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(event["event_key"])):
            raise ControlError("technical event has invalid event_key")
    decision = job["decision"]
    if decision.get("needs_user"):
        if decision.get("user_only_gate") not in VALID_USER_GATES:
            raise ControlError("needs_user requires one valid user_only_gate")
        gate = job.get("user_gate")
        if not isinstance(gate, dict):
            raise ControlError("needs_user requires a controller-created user_gate")
        if gate.get("gate_type") != decision.get("user_only_gate"):
            raise ControlError("user_gate type differs from decision gate")
        if gate.get("lifecycle") not in {"REQUIRED", "AUTHORIZED", "REFUSED", "EXPIRED"}:
            raise ControlError("user_gate has invalid lifecycle")
        if not gate.get("gate_id") or not gate.get("requested_change_sha256"):
            raise ControlError("user_gate identity is incomplete")
    tracking = job["runtime_tracking"]
    require(tracking, ("started_at", "accumulated_seconds", "last_progress_at", "legacy_runtime_unknown"), "runtime_tracking")


def dsh_lifecycle_class(dsh: dict[str, Any]) -> str:
    value = dsh.get("lifecycle_status")
    if value in TERMINAL_DSH_LIFECYCLES:
        return "terminal"
    if value in ACTIVE_DSH_LIFECYCLES:
        return "active"
    return "unknown"


def semantic_projection(job: dict[str, Any]) -> dict[str, Any]:
    controller = job.get("controller", {})
    event = controller.get("current_event") or {}
    verification = job.get("verification") or {}
    result = job.get("result") or {}
    decision = job.get("decision") or {}
    dsh = job.get("dsh") or {}
    tracking = job.get("runtime_tracking") or {}
    return {
        "status": job.get("status"),
        "authorized_specification": {
            key: job.get("authorized_specification", {}).get(key)
            for key in ("current_sha256", "protocol_revision", "protocol_authority_sha256")
        },
        "dsh": {
            "session_id": dsh.get("session_id"),
            "lifecycle": dsh_lifecycle_class(dsh),
            "continuation_required": dsh.get("continuation_required"),
            "continuation_instruction": dsh.get("continuation_instruction"),
        },
        "progress_fingerprint": tracking.get("progress_fingerprint"),
        "verification": {
            "record_version": verification.get("record_version"),
            "result": verification.get("result"),
            "review_run_id": verification.get("review_run_id"),
        },
        "decision": {
            "needs_user": decision.get("needs_user"),
            "user_only_gate": decision.get("user_only_gate"),
            "route": decision.get("route"),
        },
        "result": {
            "accepted_at": result.get("accepted_at"),
            "evidence_identity": result.get("evidence_identity"),
            "evidence_sha256": result.get("evidence_sha256"),
        },
        "root_cause_signature": event.get("root_cause_signature") or decision.get("error_code"),
        "open_technical_event_id": event.get("event_id") if event.get("lifecycle") in OPEN_EVENT_LIFECYCLES else None,
        "user_gate": {
            key: (job.get("user_gate") or {}).get(key)
            for key in ("gate_id", "gate_type", "lifecycle", "requested_change_sha256")
        },
    }


def semantic_hash(job: dict[str, Any]) -> str:
    return digest_json(semantic_projection(job))


def finalize_job_revisions(old_job: dict[str, Any], new_job: dict[str, Any]) -> None:
    controller = new_job["controller"]
    controller["state_revision"] = int(old_job["controller"]["state_revision"]) + 1
    old_semantic = int(old_job["controller"].get("semantic_revision", old_job["controller"]["state_revision"]))
    controller["semantic_revision"] = old_semantic + (semantic_hash(old_job) != semantic_hash(new_job))


def technical_event_key(job: dict[str, Any], root_cause_signature: str, resources: Any = None) -> str:
    authority = job.get("authorized_specification", {})
    identity = {
        "job_id": job["job_id"],
        "origin_status": job["status"],
        "root_cause_signature": root_cause_signature,
        "resources": resources or [],
        "authority": {
            "protocol_contract_sha256": job["controller"].get("protocol_contract_sha256"),
            "specification_sha256": authority.get("current_sha256"),
            "protocol_revision": authority.get("protocol_revision"),
            "protocol_authority_sha256": authority.get("protocol_authority_sha256"),
        },
    }
    return f"sha256:{digest_json(identity)}"


def ensure_technical_event(old_job: dict[str, Any], new_job: dict[str, Any]) -> None:
    if new_job.get("decision", {}).get("route") == "TERMINAL_TECHNICAL_FAILURE":
        return
    if new_job["status"] not in {"BLOCKED", "FAILED"} or new_job.get("decision", {}).get("needs_user"):
        return
    controller = new_job["controller"]
    root_cause = (
        new_job.get("decision", {}).get("error_code")
        or controller.get("escalation_metrics", {}).get("current_root_cause_signature")
        or "UNCLASSIFIED"
    )
    resources = new_job.get("decision", {}).get("related_resources") or []
    key = technical_event_key(new_job, str(root_cause), resources)
    current = controller.get("current_event")
    if current and current.get("event_key") == key and current.get("lifecycle") in OPEN_EVENT_LIFECYCLES:
        return
    if current and current.get("lifecycle") in OPEN_EVENT_LIFECYCLES:
        superseded = copy.deepcopy(current)
        superseded["lifecycle"] = "SUPERSEDED"
        superseded["superseded_at"] = iso_utc()
        controller.setdefault("event_history", []).append(superseded)
    generation = int(old_job["controller"].get("event_generation", 0)) + 1
    semantic_revision = int(old_job["controller"].get("semantic_revision", old_job["controller"]["state_revision"]))
    controller["event_generation"] = generation
    controller["current_event"] = {
        "event_id": f"{new_job['job_id']}:{generation}:{new_job['status']}",
        "generation": generation,
        "origin_status": new_job["status"],
        "origin_semantic_revision": semantic_revision + 1,
        "event_key": key,
        "root_cause_signature": str(root_cause),
        "resource_identities": copy.deepcopy(resources),
        "lifecycle": "REQUIRED",
        "created_at": iso_utc(),
    }


def make_user_gate(job: dict[str, Any]) -> dict[str, Any]:
    decision = job["decision"]
    requested = {
        "job_id": job["job_id"],
        "gate_type": decision["user_only_gate"],
        "requested_change": decision.get("requested_change"),
        "reason": decision.get("reason"),
    }
    requested_hash = digest_json(requested)
    return {
        "gate_id": f"gate-{requested_hash[:24]}",
        "gate_type": decision["user_only_gate"],
        "lifecycle": "REQUIRED",
        "requested_change_sha256": requested_hash,
        "created_at": iso_utc(),
    }


def validate_contracts(
    root: Path,
    runtime: dict[str, Any],
    jobs: dict[str, dict[str, Any]],
    documents: dict[str, dict[str, Any]],
) -> None:
    paths = control_paths(root)
    project_rel = relative(root, paths["project_contract"])
    if project_rel in documents:
        project_contract = documents[project_rel]
        actual_project_hash = digest_persisted_json(project_contract)
    else:
        project_contract = read_json(paths["project_contract"])
        actual_project_hash = digest_file(paths["project_contract"])
    expected_project_hash = runtime["controller"]["project_contract_sha256"]
    if actual_project_hash != expected_project_hash:
        raise ControlError("project-contract.json hash differs from runtime authority")
    if set(project_contract.get("user_only_gates", [])) != VALID_USER_GATES:
        raise ControlError("project contract has invalid user_only_gates")
    superseded_hashes = set(project_contract.get("supersedes_contract_sha256", []))

    for job_id, job in jobs.items():
        contract_path = protocol_contract_path(root, job_id)
        contract = read_json(contract_path)
        contract_hash = digest_file(contract_path)
        if job["controller"]["protocol_contract_sha256"] != contract_hash:
            raise ControlError(f"job {job_id} protocol contract hash mismatch")
        protocol_project_hash = contract.get("project_contract_sha256")
        if protocol_project_hash != expected_project_hash:
            historical_contract_is_valid = (
                job["status"] in {"ACCEPTED", "ARCHIVED"} and protocol_project_hash in superseded_hashes
            )
            if not historical_contract_is_valid:
                raise ControlError(f"job {job_id} protocol contract uses another project contract")
        specification_hash = digest_file(specification_path(root, job_id))
        if contract.get("specification_sha256") != specification_hash:
            raise ControlError(f"job {job_id} specification hash is not authorized")
        authorized = job["authorized_specification"]
        mapping = {
            "current_sha256": "specification_sha256",
            "protocol_revision": "protocol_revision",
            "protocol_authority_sha256": "protocol_authority_sha256",
            "canonical_replay_a": "canonical_replay_a",
            "canonical_replay_b": "canonical_replay_b",
            "canonical_formal": "canonical_formal",
        }
        for state_key, contract_key in mapping.items():
            if authorized.get(state_key) != contract.get(contract_key):
                raise ControlError(f"job {job_id} contract mismatch for {state_key}")


def validate_v81_acceptance(job: dict[str, Any]) -> None:
    verification = job.get("verification") or {}
    required = (
        "record_version",
        "provenance",
        "review_run_id",
        "specification_sha256",
        "protocol_contract_sha256",
        "started_at",
        "completed_at",
        "result",
        "commands",
        "unverified_items",
    )
    missing = [key for key in required if key not in verification]
    if missing:
        raise ControlError(f"ACCEPTANCE_RECORD_INCOMPLETE: {', '.join(missing)}")
    if verification["record_version"] != "V8_1" or verification["provenance"] != "LUNA_ATTESTED":
        raise ControlError("ACCEPTANCE_RECORD_INCOMPLETE: invalid record version or provenance")
    if not verification["review_run_id"] or not verification["started_at"] or not verification["completed_at"]:
        raise ControlError("ACCEPTANCE_RECORD_INCOMPLETE: review identity or timestamps are empty")
    if verification["result"] != "PASSED":
        raise ControlError("ACCEPTANCE_RECORD_INCOMPLETE: verification result is not PASSED")
    if verification["unverified_items"] != []:
        raise ControlError("ACCEPTANCE_UNVERIFIED_ITEMS_REMAIN")
    commands = verification["commands"]
    if not isinstance(commands, list) or not commands:
        raise ControlError("ACCEPTANCE_RECORD_INCOMPLETE: commands must be non-empty")
    for command in commands:
        if not isinstance(command, dict):
            raise ControlError("ACCEPTANCE_RECORD_INCOMPLETE: command entry is not an object")
        require(command, ("command_id", "command", "exit_code", "key_output", "evidence_paths", "evidence_sha256"), "verification command")
        if not command["command_id"] or not command["command"]:
            raise ControlError("ACCEPTANCE_RECORD_INCOMPLETE: command identity or text is empty")
        if command["exit_code"] != 0:
            raise ControlError("ACCEPTANCE_RECORD_INCOMPLETE: a verification command failed")
        if not isinstance(command["evidence_paths"], list) or not isinstance(command["evidence_sha256"], list):
            raise ControlError("ACCEPTANCE_RECORD_INCOMPLETE: invalid command evidence")
    if (
        verification["specification_sha256"] != job["authorized_specification"]["current_sha256"]
        or verification["protocol_contract_sha256"] != job["controller"]["protocol_contract_sha256"]
    ):
        raise ControlError("ACCEPTANCE_AUTHORITY_MISMATCH")


def validate_legacy_acceptance(root: Path, job: dict[str, Any]) -> None:
    if job["status"] not in {"ACCEPTED", "ARCHIVED"} or not job.get("result", {}).get("accepted_at"):
        raise ControlError("LEGACY_ACCEPTANCE_INVALID: job is not accepted")
    verification = job.get("verification") or {}
    if verification.get("record_version") != "LEGACY_V8":
        raise ControlError("LEGACY_ACCEPTANCE_INVALID: missing legacy marker")
    migration = verification.get("legacy_migration") or {}
    transaction_id = migration.get("original_accept_transaction_id")
    original_hash = migration.get("original_verification_sha256")
    if not transaction_id or not original_hash:
        raise ControlError("LEGACY_ACCEPTANCE_INVALID: migration receipt is incomplete")
    transaction = _validated_committed_transaction(control_paths(root)["transactions"] / transaction_id)
    if transaction is None:
        raise ControlError("LEGACY_ACCEPTANCE_INVALID: original transaction is not committed")
    historical = transaction[1].get("documents", {}).get(f"jobs/{job['job_id']}/state.json")
    if not isinstance(historical, dict) or historical.get("status") not in {"ACCEPTED", "ARCHIVED"}:
        raise ControlError("LEGACY_ACCEPTANCE_INVALID: original accepted snapshot is absent")
    if digest_json(historical.get("verification") or {}) != original_hash:
        raise ControlError("LEGACY_ACCEPTANCE_INVALID: verification hash mismatch")
    preserved = copy.deepcopy(verification)
    preserved.pop("record_version", None)
    preserved.pop("legacy_migration", None)
    if digest_json(preserved) != migration.get("pre_migration_verification_sha256"):
        raise ControlError("LEGACY_ACCEPTANCE_INVALID: pre-migration verification content changed")
    contract = read_json(protocol_contract_path(root, job["job_id"]))
    if contract.get("specification_sha256") != job["authorized_specification"].get("current_sha256"):
        raise ControlError("LEGACY_ACCEPTANCE_INVALID: specification authority mismatch")
    if digest_file(protocol_contract_path(root, job["job_id"])) != job["controller"].get("protocol_contract_sha256"):
        raise ControlError("LEGACY_ACCEPTANCE_INVALID: protocol authority mismatch")


def validate_acceptance(root: Path, job: dict[str, Any]) -> None:
    version = (job.get("verification") or {}).get("record_version")
    if version == "V8_1":
        validate_v81_acceptance(job)
    elif version == "LEGACY_V8":
        validate_legacy_acceptance(root, job)
    else:
        raise ControlError("ACCEPTANCE_RECORD_INCOMPLETE: unknown acceptance record version")


def validate_project_completion(
    root: Path,
    runtime: dict[str, Any],
    queue: dict[str, Any],
    jobs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    gaps: list[dict[str, Any]] = []
    if runtime.get("project_status") != "ACTIVE":
        gaps.append({"code": "PROJECT_NOT_ACTIVE", "value": runtime.get("project_status")})
    if runtime.get("active_job_id") is not None:
        gaps.append({"code": "ACTIVE_JOB_REMAINS", "job_id": runtime.get("active_job_id")})
    if queue.get("jobs"):
        gaps.append({"code": "QUEUE_NOT_EMPTY", "job_ids": [entry.get("job_id") for entry in queue["jobs"]]})
    project_contract = read_json(control_paths(root)["project_contract"])
    try:
        validate_contracts(root, runtime, jobs, collect_documents(root))
    except ControlError as exc:
        gaps.append({"code": "AUTHORITY_CHAIN_INVALID", "detail": str(exc)})
    required = project_contract.get("required_queue_order")
    if not isinstance(required, list):
        gaps.append({"code": "PROJECT_REQUIRED_QUEUE_ORDER_MISSING"})
        required = []
    for job_id in required:
        job = jobs.get(job_id)
        if job is None:
            gaps.append({"code": "REQUIRED_JOB_MISSING", "job_id": job_id})
            continue
        if job["status"] not in {"ACCEPTED", "ARCHIVED"}:
            gaps.append({"code": "REQUIRED_JOB_NOT_ACCEPTED", "job_id": job_id, "status": job["status"]})
            continue
        try:
            validate_acceptance(root, job)
        except ControlError as exc:
            gaps.append({"code": "REQUIRED_JOB_ACCEPTANCE_INVALID", "job_id": job_id, "detail": str(exc)})
    pending_intents = unresolved_intents(root)
    if pending_intents:
        gaps.append({"code": "UNRESOLVED_INTENT", "intent_ids": [item.get("intent_id") for item in pending_intents]})
    queue_event = queue.get("last_empty_event") or {}
    if queue_event.get("status") in {"REQUIRED", "DISPATCHED", "ACKNOWLEDGED"}:
        gaps.append({"code": "OPEN_TECHNICAL_EVENT", "event_id": queue_event.get("event_id"), "scope": "project"})
    for job_id, job in jobs.items():
        event = job.get("controller", {}).get("current_event") or {}
        if event.get("lifecycle") in OPEN_EVENT_LIFECYCLES:
            gaps.append({"code": "OPEN_TECHNICAL_EVENT", "job_id": job_id, "event_id": event.get("event_id")})
        gate = job.get("user_gate") or {}
        if gate.get("lifecycle") in OPEN_USER_GATE_LIFECYCLES:
            gaps.append({"code": "OPEN_USER_GATE", "job_id": job_id, "gate_id": gate.get("gate_id")})
        dsh = job.get("dsh") or {}
        if dsh.get("session_id") and dsh_lifecycle_class(dsh) != "terminal":
            gaps.append(
                {
                    "code": "EXTERNAL_SESSION_STATE_UNVERIFIED",
                    "job_id": job_id,
                    "session_id": dsh.get("session_id"),
                    "lifecycle_status": dsh.get("lifecycle_status"),
                }
            )
    return {"ok": not gaps, "required_jobs": required, "gaps": gaps}


def validate_all(
    root: Path,
    documents: dict[str, dict[str, Any]],
    *,
    check_contracts: bool = True,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, dict[str, Any]]]:
    runtime = documents["control/runtime.json"]
    queue = documents["control/queue.json"]
    validate_runtime(runtime)
    validate_queue(queue)
    jobs: dict[str, dict[str, Any]] = {}
    for rel_path, document in documents.items():
        if rel_path.startswith("jobs/") and rel_path.endswith("/state.json"):
            validate_job(document)
            jobs[document["job_id"]] = document
    queue_ids = [entry["job_id"] for entry in queue["jobs"]]
    missing = [job_id for job_id in queue_ids if job_id not in jobs]
    if missing:
        raise ControlError(f"queue references missing jobs: {', '.join(missing)}")
    active_job_id = runtime.get("active_job_id")
    if active_job_id is not None:
        if active_job_id not in jobs:
            raise ControlError(f"active job does not exist: {active_job_id}")
        if active_job_id not in queue_ids and jobs[active_job_id]["status"] not in {"ACCEPTED", "ARCHIVED"}:
            raise ControlError("active non-terminal job is absent from queue")
    if check_contracts:
        validate_contracts(root, runtime, jobs, documents)
        for job in jobs.values():
            if job["status"] in {"ACCEPTED", "ARCHIVED"}:
                validate_acceptance(root, job)
    return runtime, queue, jobs


def unresolved_intents(root: Path) -> list[dict[str, Any]]:
    directory = control_paths(root)["intents"]
    if not directory.exists():
        return []
    values: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".resolved.json"):
            continue
        intent = read_json(path)
        marker = directory / f"{intent['intent_id']}.resolved.json"
        if not marker.exists():
            values.append(intent)
    return values


def escalation_allowed(runtime: dict[str, Any], job: dict[str, Any]) -> tuple[bool, str | None]:
    metrics = job["controller"]["escalation_metrics"]
    total = int(metrics.get("total_dispatches", 0))
    if total >= int(runtime["defaults"]["max_sol_escalations_per_job"]):
        return False, "SOL_ESCALATION_BUDGET_EXHAUSTED"
    root_causes = metrics.get("root_causes", {})
    current_signature = (
        (job["controller"].get("current_event") or {}).get("root_cause_signature")
        or job.get("decision", {}).get("error_code")
        or metrics.get("current_root_cause_signature")
    )
    if current_signature and int(root_causes.get(current_signature, 0)) >= int(runtime["defaults"]["max_same_root_cause_escalations"]):
        return False, "SAME_ROOT_CAUSE_BUDGET_EXHAUSTED"
    return True, None


def determine_action(root: Path, runtime: dict[str, Any], queue: dict[str, Any], jobs: dict[str, dict[str, Any]]) -> dict[str, Any]:
    pending = unresolved_intents(root)
    if pending:
        return {"type": "RECOVER_INTENT", "intent": pending[0], "pending_count": len(pending)}

    if runtime["project_status"] == "COMPLETED":
        return {"type": "PROJECT_COMPLETED"}
    if runtime["project_status"] in {"FAILED", "PAUSED"}:
        return {"type": "NO_ACTION", "reason": f"PROJECT_{runtime['project_status']}"}

    active_job_id = runtime.get("active_job_id")
    if active_job_id is None:
        if queue["jobs"]:
            return {"type": "ACTIVATE_JOB", "job_id": queue["jobs"][0]["job_id"]}
        completion = validate_project_completion(root, runtime, queue, jobs)
        if completion["ok"]:
            return {"type": "COMPLETE_PROJECT", "completion": completion}
        open_gate = next((gap for gap in completion["gaps"] if gap["code"] == "OPEN_USER_GATE"), None)
        if open_gate:
            return {"type": "USER_REQUIRED", "job_id": open_gate["job_id"], "gate_id": open_gate["gate_id"]}
        generation = queue["planning_generation"]
        event_id = f"project:{queue['queue_revision']}:{generation}:QUEUE_EMPTY"
        previous = queue.get("last_empty_event") or {}
        if previous.get("event_id") == event_id and previous.get("status") == "DISPATCHED":
            thread_id = previous.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id.strip():
                return {"type": "INVALID_STATE", "reason": "OPEN_EVENT_WITHOUT_SOL_THREAD_ID", "event_id": event_id}
            last_checked = previous.get("status_checked_at")
            if last_checked:
                retry_at = parse_time(last_checked) + timedelta(minutes=int(runtime["defaults"]["check_interval_minutes"]))
                if utc_now() < retry_at:
                    return {
                        "type": "NO_ACTION",
                        "reason": "SOL_THREAD_RECENTLY_CHECKED",
                        "event_id": event_id,
                        "thread_id": thread_id,
                        "retry_at": iso_utc(retry_at),
                    }
            return {
                "type": "SOL_STATUS",
                "reason": "OPEN_EVENT_REQUIRES_SOL_STATUS",
                "event_id": event_id,
                "thread_id": thread_id,
                "event_scope": "project",
            }
        if previous.get("event_id") == event_id and previous.get("status") in {"RESOLVED", "USER_REQUIRED"}:
            return {"type": "NO_ACTION", "reason": "QUEUE_EMPTY_EVENT_ALREADY_HANDLED", "event_id": event_id}
        return {"type": "SOL_QUEUE_EMPTY", "event_id": event_id, "gaps": completion["gaps"]}

    job = jobs[active_job_id]
    status = job["status"]
    state_token = f"{active_job_id}:{job['controller']['semantic_revision']}:{status}"
    dsh = job["dsh"]

    tracking = job["runtime_tracking"]
    active_seconds = float(tracking.get("accumulated_seconds", 0))
    if tracking.get("started_at"):
        active_seconds += max(0.0, (utc_now() - parse_time(tracking["started_at"])).total_seconds())
    max_seconds = float(job["limits"].get("max_auto_runtime_hours", runtime["defaults"]["max_auto_runtime_hours"])) * 3600
    if active_seconds >= max_seconds and status not in {"ACCEPTED", "ARCHIVED", "CANCELLED"}:
        return {
            "type": "LIMIT_REACHED",
            "job_id": active_job_id,
            "state_token": state_token,
            "reason": "MAX_AUTO_RUNTIME_REACHED",
            "active_seconds": round(active_seconds, 3),
            "limit_seconds": max_seconds,
        }

    if status == "QUEUED":
        if dsh.get("session_id"):
            if dsh.get("continuation_required") and job["decision"].get("route") == "CONTINUE_SAME_DSH_SESSION":
                return {
                    "type": "DSH_CONTINUE",
                    "job_id": active_job_id,
                    "state_token": state_token,
                    "session_id": dsh["session_id"],
                    "workspace": job["workspace"],
                    "continuation_instruction": dsh.get("continuation_instruction"),
                }
            return {"type": "INVALID_STATE", "reason": "QUEUED_WITH_SESSION_BUT_NO_VALID_CONTINUATION", "state_token": state_token}
        return {
            "type": "DSH_START",
            "job_id": active_job_id,
            "state_token": state_token,
            "workspace": job["workspace"],
            "mode": dsh["mode"],
            "specification": relative(root, specification_path(root, active_job_id)),
        }
    if status == "RUNNING":
        if not dsh.get("session_id"):
            return {"type": "INVALID_STATE", "reason": "RUNNING_WITHOUT_SESSION", "state_token": state_token}
        stale = False
        last_progress = tracking.get("last_progress_at")
        if last_progress:
            stale = utc_now() - parse_time(last_progress) >= timedelta(minutes=int(runtime["defaults"]["stale_after_minutes"]))
        return {
            "type": "DSH_STATUS",
            "job_id": active_job_id,
            "state_token": state_token,
            "session_id": dsh["session_id"],
            "stale_suspected": stale,
            "last_progress_at": last_progress,
        }
    if status == "REVIEW_PENDING":
        return {
            "type": "REVIEW",
            "job_id": active_job_id,
            "state_token": state_token,
            "specification": relative(root, specification_path(root, active_job_id)),
            "protocol_contract": relative(root, protocol_contract_path(root, active_job_id)),
        }
    if status == "ACCEPTED":
        return {"type": "HANDOFF_ACCEPTED_JOB", "job_id": active_job_id, "state_token": state_token}
    if status in {"BLOCKED", "FAILED"}:
        decision = job["decision"]
        if decision.get("route") == "TERMINAL_TECHNICAL_FAILURE":
            return {"type": "TERMINAL_TECHNICAL_FAILURE", "job_id": active_job_id, "reason": decision.get("reason")}
        if decision.get("needs_user"):
            gate = job.get("user_gate") or {}
            return {"type": "USER_REQUIRED", "job_id": active_job_id, "gate": decision["user_only_gate"], "gate_id": gate.get("gate_id")}
        event = job["controller"].get("current_event") or {}
        if event.get("lifecycle") in {"DISPATCHED", "ACKNOWLEDGED"}:
            thread_id = event.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id.strip():
                return {
                    "type": "INVALID_STATE",
                    "reason": "OPEN_EVENT_WITHOUT_SOL_THREAD_ID",
                    "event_id": event.get("event_id"),
                    "state_token": state_token,
                }
            last_checked = event.get("status_checked_at")
            user_retry_at = event.get("user_retry_requested_at")
            if last_checked and (not user_retry_at or parse_time(last_checked) >= parse_time(user_retry_at)):
                retry_at = parse_time(last_checked) + timedelta(minutes=int(runtime["defaults"]["check_interval_minutes"]))
                if utc_now() < retry_at:
                    return {
                        "type": "NO_ACTION",
                        "reason": "SOL_THREAD_RECENTLY_CHECKED",
                        "event_id": event.get("event_id"),
                        "thread_id": thread_id,
                        "retry_at": iso_utc(retry_at),
                    }
            return {
                "type": "SOL_STATUS",
                "reason": "OPEN_EVENT_REQUIRES_SOL_STATUS",
                "job_id": active_job_id,
                "event_id": event.get("event_id"),
                "thread_id": thread_id,
                "event_lifecycle": event.get("lifecycle"),
            }
        if event.get("lifecycle") not in OPEN_EVENT_LIFECYCLES:
            return {"type": "INVALID_STATE", "reason": "BLOCKED_WITHOUT_OPEN_TECHNICAL_EVENT", "state_token": state_token}
        allowed, reason = escalation_allowed(runtime, job)
        if not allowed:
            return {"type": "TERMINAL_TECHNICAL_FAILURE", "job_id": active_job_id, "event_id": event.get("event_id"), "reason": reason}
        return {"type": "SOL_ESCALATE", "job_id": active_job_id, "event_id": event.get("event_id")}
    if status == "CANCELLED":
        return {"type": "TERMINAL_CANCELLED", "job_id": active_job_id, "state_token": state_token}
    if status == "DRAFT":
        return {"type": "INVALID_STATE", "reason": "ACTIVE_JOB_IS_DRAFT", "state_token": state_token}
    return {"type": "NO_ACTION", "reason": f"STATUS_{status}"}


def determine_sol_action(
    runtime: dict[str, Any],
    queue: dict[str, Any],
    jobs: dict[str, dict[str, Any]],
    event_id: str | None,
    source_thread_id: str | None = None,
) -> dict[str, Any]:
    if not event_id:
        return {"type": "NO_ACTION_STALE_EVENT", "reason": "SOL_EVENT_ID_REQUIRED"}
    if event_id.startswith("project:"):
        expected = f"project:{queue['queue_revision']}:{queue['planning_generation']}:QUEUE_EMPTY"
        if event_id != expected or runtime.get("active_job_id") is not None or queue["jobs"]:
            return {"type": "NO_ACTION_STALE_EVENT", "event_id": event_id, "expected": expected}
        escalation = queue.get("last_empty_event") or {}
        if escalation.get("event_id") != event_id or escalation.get("status") != "DISPATCHED":
            return {"type": "NO_ACTION_STALE_EVENT", "event_id": event_id, "reason": "QUEUE_EMPTY_EVENT_NOT_DISPATCHED"}
        recorded_thread_id = escalation.get("thread_id")
        if not isinstance(recorded_thread_id, str) or not recorded_thread_id.strip():
            return {"type": "NO_ACTION_STALE_EVENT", "event_id": event_id, "reason": "SOL_THREAD_ID_MISSING"}
        if source_thread_id and source_thread_id != recorded_thread_id:
            return {
                "type": "NO_ACTION_STALE_EVENT",
                "event_id": event_id,
                "reason": "SOL_THREAD_ID_MISMATCH",
                "expected_thread_id": recorded_thread_id,
            }
        return {"type": "SOL_HANDLE_QUEUE_EMPTY", "event_id": event_id, "thread_id": recorded_thread_id}
    job_id = event_id.split(":", 1)[0]
    job = jobs.get(job_id)
    if job is None or job["status"] not in {"BLOCKED", "FAILED"}:
        return {"type": "NO_ACTION_STALE_EVENT", "event_id": event_id}
    event = job["controller"].get("current_event") or {}
    if event.get("event_id") != event_id or event.get("lifecycle") not in {"DISPATCHED", "ACKNOWLEDGED"}:
        return {"type": "NO_ACTION_STALE_EVENT", "event_id": event_id}
    recorded_thread_id = event.get("thread_id")
    if not isinstance(recorded_thread_id, str) or not recorded_thread_id.strip():
        return {"type": "NO_ACTION_STALE_EVENT", "event_id": event_id, "reason": "SOL_THREAD_ID_MISSING"}
    if source_thread_id and source_thread_id != recorded_thread_id:
        return {
            "type": "NO_ACTION_STALE_EVENT",
            "event_id": event_id,
            "reason": "SOL_THREAD_ID_MISMATCH",
            "expected_thread_id": recorded_thread_id,
        }
    return {
        "type": "SOL_HANDLE_EVENT",
        "event_id": event_id,
        "job_id": job_id,
        "status": job["status"],
        "workspace": job["workspace"],
        "specification": f"jobs/{job_id}/specification.md",
        "protocol_contract": f"jobs/{job_id}/protocol-contract.json",
        "thread_id": recorded_thread_id,
    }


def lease_is_current(root: Path, token: str) -> dict[str, Any]:
    lease = read_json(control_paths(root)["lease"])
    if lease.get("token") != token:
        raise ControlError("lease token is no longer current")
    if parse_time(lease["expires_at"]) <= utc_now():
        raise ControlError("lease has expired")
    return lease


def acquire_lease(
    root: Path,
    role: str,
    source_thread_id: str | None,
    event_id: str | None,
    wait_lease_seconds: float = 0,
) -> dict[str, Any]:
    if role not in ROLE_VALUES:
        raise ControlError(f"invalid role: {role}")
    if wait_lease_seconds < 0 or wait_lease_seconds > 300:
        raise ControlError("wait_lease_seconds must be between 0 and 300")
    if role != "SOL" and wait_lease_seconds:
        raise ControlError("only Sol may wait for a predecessor lease")
    paths = control_paths(root)
    paths["transactions"].mkdir(parents=True, exist_ok=True)
    paths["intents"].mkdir(parents=True, exist_ok=True)
    lease_path = paths["lease"]

    wait_deadline = time.monotonic() + wait_lease_seconds
    while lease_path.exists():
        existing = read_json(lease_path)
        if parse_time(existing["expires_at"]) > utc_now():
            remaining = wait_deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(0.1, remaining))
                continue
            return {
                "ok": False,
                "action": {"type": "NO_ACTION", "reason": "LEASE_HELD"},
                "held_by": {key: existing.get(key) for key in ("run_id", "role", "started_at", "expires_at")},
            }
        stale_dir = root / "control" / "stale-leases"
        stale_dir.mkdir(parents=True, exist_ok=True)
        stale_path = stale_dir / f"{existing.get('run_id', 'unknown')}-{uuid.uuid4().hex[:8]}.json"
        try:
            os.replace(lease_path, stale_path)
        except FileNotFoundError:
            continue

    started = utc_now()
    # Read only the TTL before acquiring. Recovery and every other state read
    # happen after the exclusive create so two stale-lock contenders cannot
    # both repair or plan against the store.
    runtime_hint = read_json(paths["runtime"])
    ttl = int(runtime_hint.get("defaults", {}).get("lease_ttl_minutes", 60))
    lease = {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "token": str(uuid.uuid4()),
        "role": role,
        "source_thread_id": source_thread_id,
        "bound_event_id": event_id,
        "started_at": iso_utc(started),
        "expires_at": iso_utc(started + timedelta(minutes=ttl)),
        "snapshot_hashes": {},
        "planned_action": {"type": "INITIALIZING"},
    }
    try:
        exclusive_write_json(lease_path, lease)
    except ControlError:
        remaining = wait_deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.1, remaining))
            return acquire_lease(root, role, source_thread_id, event_id, remaining)
        existing = read_json(lease_path)
        return {
            "ok": False,
            "action": {"type": "NO_ACTION", "reason": "LEASE_RACE_LOST"},
            "held_by": {key: existing.get(key) for key in ("run_id", "role", "started_at", "expires_at")},
        }
    try:
        recovered = recover_transactions(root)
        documents = collect_documents(root)
        verify_transaction_snapshot(root, documents)
        runtime, queue, jobs = validate_all(root, documents)
        lease["snapshot_hashes"] = document_hashes(documents)
        if role == "SOL":
            lease["planned_action"] = determine_sol_action(runtime, queue, jobs, event_id, source_thread_id)
            if not lease.get("source_thread_id") and lease["planned_action"].get("type") in {
                "SOL_HANDLE_EVENT",
                "SOL_HANDLE_QUEUE_EMPTY",
            }:
                # The desktop task cannot always discover its own ID. Preserve the
                # dispatch binding in the lease so interrupted-run recovery and
                # audit logs still identify the intended visible task.
                lease["source_thread_id"] = lease["planned_action"].get("thread_id")
        else:
            lease["planned_action"] = determine_action(root, runtime, queue, jobs)
        active_job_id = runtime.get("active_job_id")
        lease["expected"] = {
            "runtime_updated_at": runtime["updated_at"],
            "queue_revision": queue["queue_revision"],
            "job_state_revision": jobs[active_job_id]["controller"]["state_revision"] if active_job_id else None,
            "job_status": jobs[active_job_id]["status"] if active_job_id else None,
        }
        lease["request_path"] = f"control/requests/{lease['run_id']}.json"
        deadline_seconds = int(runtime["defaults"].get("routing_action_deadline_seconds", 75))
        lease["routing_action_deadline_at"] = iso_utc(started + timedelta(seconds=deadline_seconds))
        atomic_write_json(lease_path, lease)
    except Exception:
        if lease_path.exists():
            try:
                current = read_json(lease_path)
                if current.get("token") == lease["token"]:
                    lease_path.unlink()
            except Exception:
                pass
        raise

    if lease["planned_action"]["type"] == "NO_ACTION_STALE_EVENT":
        if lease_path.exists() and read_json(lease_path).get("token") == lease["token"]:
            lease_path.unlink()
        return {"ok": False, "action": lease["planned_action"]}

    output = copy.deepcopy(lease)
    output.pop("token")
    output["lease_token"] = lease["token"]
    output["ok"] = True
    output["recovered_transactions"] = recovered
    return output


def renew_lease(root: Path, token: str) -> dict[str, Any]:
    lease = lease_is_current(root, token)
    runtime = read_json(control_paths(root)["runtime"])
    lease["expires_at"] = iso_utc(utc_now() + timedelta(minutes=int(runtime["defaults"]["lease_ttl_minutes"])))
    atomic_write_json(control_paths(root)["lease"], lease)
    return {"ok": True, "run_id": lease["run_id"], "expires_at": lease["expires_at"]}


def intent_key(action: dict[str, Any], snapshot_hashes: dict[str, str]) -> str:
    identity = {
        "type": action["type"],
        "event_id": action.get("event_id"),
        "job_id": action.get("job_id"),
        "session_id": action.get("session_id"),
        "snapshot_hashes": snapshot_hashes,
    }
    return digest_json(identity)[:24]


def prepare_intent(root: Path, token: str) -> dict[str, Any]:
    lease = lease_is_current(root, token)
    action = lease["planned_action"]
    if action["type"] not in EXTERNAL_ACTIONS:
        raise ControlError(f"action {action['type']} does not need an external intent")
    if action["type"] == "RECOVER_INTENT":
        return {"ok": True, "intent": action["intent"], "reused": True}
    intent_id = intent_key(action, lease["snapshot_hashes"])
    path = control_paths(root)["intents"] / f"{intent_id}.json"
    if path.exists():
        return {"ok": True, "intent": read_json(path), "reused": True}
    pending = unresolved_intents(root)
    if pending:
        raise ControlError("another unresolved intent must be recovered first")
    intent = {
        "schema_version": 1,
        "intent_id": intent_id,
        "correlation_id": f"longtime:{intent_id}",
        "run_id": lease["run_id"],
        "role": lease["role"],
        "created_at": iso_utc(),
        "action": action,
        "snapshot_hashes": lease["snapshot_hashes"],
    }
    exclusive_write_json(path, intent)
    lease["intent_id"] = intent_id
    atomic_write_json(control_paths(root)["lease"], lease)
    return {"ok": True, "intent": intent, "reused": False}


def deep_merge(base: Any, patch: Any) -> Any:
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    result = copy.deepcopy(base)
    for key, value in patch.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def validate_successor_readiness(
    root: Path,
    current_job_id: str,
    successor_entry: dict[str, Any],
    jobs: dict[str, dict[str, Any]],
) -> None:
    successor_id = successor_entry.get("job_id")
    successor = jobs.get(successor_id)
    if successor is None or successor.get("status") != "QUEUED":
        raise ControlError("HANDOFF_SUCCESSOR_NOT_QUEUED")
    project = read_json(control_paths(root)["project_contract"])
    order = project.get("required_queue_order")
    if not isinstance(order, list) or current_job_id not in order or successor_id not in order:
        raise ControlError("HANDOFF_SUCCESSOR_NOT_IN_PROJECT_CONTRACT")
    if order.index(successor_id) != order.index(current_job_id) + 1:
        raise ControlError("HANDOFF_SUCCESSOR_OUT_OF_ORDER")
    for predecessor_id in order[: order.index(successor_id)]:
        predecessor = jobs.get(predecessor_id)
        if predecessor is None or predecessor.get("status") not in {"ACCEPTED", "ARCHIVED"}:
            raise ControlError(f"HANDOFF_PREDECESSOR_NOT_ACCEPTED: {predecessor_id}")
    contract_path = protocol_contract_path(root, successor_id)
    specification = specification_path(root, successor_id)
    contract = read_json(contract_path)
    declared = contract.get("predecessors") or []
    for predecessor_id in declared:
        predecessor = jobs.get(predecessor_id)
        if predecessor is None or predecessor.get("status") not in {"ACCEPTED", "ARCHIVED"}:
            raise ControlError(f"HANDOFF_DECLARED_PREDECESSOR_NOT_ACCEPTED: {predecessor_id}")
    contract_hash = digest_file(contract_path)
    specification_hash = digest_file(specification)
    if contract.get("specification_sha256") != specification_hash:
        raise ControlError("HANDOFF_SUCCESSOR_SPECIFICATION_MISMATCH")
    if successor["controller"].get("protocol_contract_sha256") != contract_hash:
        raise ControlError("HANDOFF_SUCCESSOR_PROTOCOL_MISMATCH")
    if successor_entry.get("specification_sha256") != specification_hash:
        raise ControlError("HANDOFF_QUEUE_SPECIFICATION_MISMATCH")
    if successor_entry.get("protocol_contract_sha256") != contract_hash:
        raise ControlError("HANDOFF_QUEUE_PROTOCOL_MISMATCH")
    dsh = successor.get("dsh") or {}
    if dsh.get("session_id") and not (
        dsh.get("continuation_required")
        and successor.get("decision", {}).get("route") == "CONTINUE_SAME_DSH_SESSION"
    ):
        raise ControlError("HANDOFF_SUCCESSOR_SESSION_CONFLICT")


def apply_handoff_accepted_job(
    root: Path,
    current: dict[str, dict[str, Any]],
    runtime: dict[str, Any],
    queue: dict[str, Any],
    jobs: dict[str, dict[str, Any]],
    job_id: str,
) -> dict[str, dict[str, Any]]:
    if runtime.get("active_job_id") != job_id or jobs[job_id].get("status") != "ACCEPTED":
        raise ControlError("HANDOFF_ACCEPTED_PRECONDITION_FAILED")
    queue_ids = [entry.get("job_id") for entry in queue["jobs"]]
    if not queue_ids or queue_ids[0] != job_id:
        raise ControlError("HANDOFF_CURRENT_JOB_QUEUE_POSITION_INVALID")
    remaining = copy.deepcopy(queue["jobs"][1:])
    if remaining:
        validate_successor_readiness(root, job_id, remaining[0], jobs)

    updated = copy.deepcopy(current)
    now = iso_utc()
    archived = copy.deepcopy(jobs[job_id])
    archived["status"] = "ARCHIVED"
    archived["updated_at"] = now
    finalize_job_revisions(jobs[job_id], archived)
    updated[f"jobs/{job_id}/state.json"] = archived
    updated["control/queue.json"]["jobs"] = remaining
    updated["control/queue.json"]["queue_revision"] = int(queue["queue_revision"]) + 1
    updated["control/queue.json"]["updated_at"] = now
    updated["control/runtime.json"]["active_job_id"] = remaining[0]["job_id"] if remaining else None
    if not remaining:
        runtime_after, queue_after, jobs_after = validate_all(root, updated)
        completion = validate_project_completion(root, runtime_after, queue_after, jobs_after)
        if completion["ok"]:
            updated["control/runtime.json"]["project_status"] = "COMPLETED"
        else:
            updated["control/runtime.json"].setdefault("controller", {})["completion_gaps"] = completion["gaps"]
    return updated


def apply_request(
    root: Path,
    lease: dict[str, Any],
    request: dict[str, Any],
    *,
    protocol_file_updates: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    protocol_file_updates = protocol_file_updates or {}
    current = collect_documents(root)
    if document_hashes(current) != lease["snapshot_hashes"]:
        raise ControlError("state changed after begin; discard this run")
    adopt_protocol = bool(request.get("adopt_protocol_contract"))
    protocol_migrations = request.get("protocol_migrations")
    runtime, queue, jobs = validate_all(
        root,
        current,
        check_contracts=not (adopt_protocol or protocol_migrations is not None),
    )

    expected = request.get("expected", {})
    if expected.get("runtime_updated_at") not in {None, runtime["updated_at"]}:
        raise ControlError("runtime_updated_at compare-and-swap failed")
    if expected.get("queue_revision") not in {None, queue["queue_revision"]}:
        raise ControlError("queue_revision compare-and-swap failed")
    job_id = request.get("job_id") or runtime.get("active_job_id")
    if expected.get("job_state_revision") is not None:
        if not job_id or jobs[job_id]["controller"]["state_revision"] != expected["job_state_revision"]:
            raise ControlError("job_state_revision compare-and-swap failed")
    if expected.get("job_status") is not None:
        if not job_id or jobs[job_id]["status"] != expected["job_status"]:
            raise ControlError("job_status compare-and-swap failed")

    patches = request.get("patches", {})
    if not isinstance(patches, dict):
        raise ControlError("patches must be an object")
    role = lease["role"]
    runtime_patch = patches.get("runtime", {})
    queue_patch = patches.get("queue", {})
    job_patch = patches.get("job", {})
    new_job_request = request.get("new_job")
    new_jobs_request = request.get("new_jobs")
    project_contract_request = request.get("project_contract")
    event_update = request.get("event_update")
    user_gate_update = request.get("user_gate_update")
    queue_event_update = request.get("queue_event_update")
    maintenance_resolution = request.get("maintenance_resolution")
    handler_retry = request.get("handler_retry")
    if handler_retry is not None:
        if role not in {"USER", "MIGRATION"}:
            raise ControlError("HANDLER_RETRY_REQUIRES_USER")
        event = (jobs.get(job_id, {}).get("controller", {}).get("current_event") or {})
        if (not isinstance(handler_retry, dict) or not handler_retry.get("authorization_basis")
                or runtime["project_status"] != "ACTIVE" or jobs.get(job_id, {}).get("status") not in {"BLOCKED", "FAILED"}
                or event.get("lifecycle") not in {"DISPATCHED", "ACKNOWLEDGED"}
                or handler_retry.get("event_id") != event.get("event_id")
                or not event.get("thread_id") or handler_retry.get("thread_id") != event.get("thread_id")
                or (jobs.get(job_id, {}).get("decision") or {}).get("needs_user")
                or any((patches, event_update, queue_event_update, user_gate_update, maintenance_resolution,
                        new_job_request, new_jobs_request, project_contract_request, adopt_protocol, protocol_migrations,
                        request.get("resolve_intent"), request.get("retain_lease")))):
            raise ControlError("HANDLER_RETRY_IDENTITY_OR_SCOPE_INVALID")
    capability_required = runtime["controller"].get("handler_full_access_required", False)
    capability_failure = request.get("finish", {}).get("outcome") == "DISPATCH_CAPABILITY_MISMATCH"
    capability_reference = None
    handler_checkpoint = (event_update or queue_event_update or {}).get("lifecycle") == "ACKNOWLEDGED"
    if capability_required and ((event_update or queue_event_update or {}).get("lifecycle") == "DISPATCHED"
                                or (event_update or queue_event_update or {}).get("recovery_requested_at")):
        resolution = request.get("resolve_intent") or {}
        planned_dispatch = lease["planned_action"]
        required_intent_id = (planned_dispatch.get("intent") or {}).get("intent_id") or lease.get("intent_id")
        effective_action = (planned_dispatch.get("intent") or {}).get("action") or planned_dispatch
        dispatch_thread = (event_update or queue_event_update).get("thread_id") or effective_action.get("thread_id")
        if (not required_intent_id or resolution.get("intent_id") != required_intent_id
                or resolution.get("external_id") != dispatch_thread):
            raise ControlError("HANDLER_DISPATCH_INTENT_REQUIRED")
        from control.exception_handler import validate_dispatch_receipt
        validate_dispatch_receipt(root, request.get("dispatch_receipt"), effective_action.get("event_id"),
                                  dispatch_thread, required_intent_id)
    if capability_required and role == "SOL":
        from control.handler_capabilities import validate_preflight
        capability_reference = validate_preflight(root, lease, passed=not capability_failure)
        if capability_failure:
            if any((runtime_patch, queue_patch, job_patch, event_update, queue_event_update, user_gate_update,
                    adopt_protocol, protocol_migrations, new_job_request, new_jobs_request, project_contract_request,
                    request.get("resolve_intent"), request.get("retain_lease"))):
                raise ControlError("CAPABILITY_FAILURE_MUST_PRESERVE_EVENT_AND_STATE")
        elif not handler_checkpoint:
            bound = ((jobs.get(job_id, {}).get("controller", {}).get("current_event") or {})
                     if lease["planned_action"]["type"] == "SOL_HANDLE_EVENT" else (queue.get("last_empty_event") or {}))
            if not bound.get("acknowledged_at"):
                raise ControlError("HANDLER_ACKNOWLEDGEMENT_REQUIRED")
    if maintenance_resolution is not None:
        if role not in {"USER", "MIGRATION"}:
            raise ControlError("MAINTENANCE_RESOLUTION_REQUIRES_USER")
        if not isinstance(maintenance_resolution, dict) or not job_id:
            raise ControlError("MAINTENANCE_RESOLUTION_INVALID")
        event = jobs[job_id]["controller"].get("current_event") or {}
        if (runtime["project_status"] != "PAUSED"
                or event.get("lifecycle") != "ACKNOWLEDGED"
                or maintenance_resolution.get("event_id") != event.get("event_id")
                or maintenance_resolution.get("thread_id") != event.get("thread_id")
                or not maintenance_resolution.get("authorization_basis")):
            raise ControlError("MAINTENANCE_RESOLUTION_IDENTITY_OR_AUTHORITY_INVALID")
        if (set(patches) != {"runtime", "job"}
                or runtime_patch != {"project_status": "ACTIVE"}
                or set(job_patch) - {"status", "dsh", "decision"}
                or job_patch.get("status") != "QUEUED"
                or job_patch.get("dsh", {}).get("continuation_required") is not True
                or not job_patch.get("dsh", {}).get("continuation_instruction")
                or not jobs[job_id]["dsh"].get("session_id")
                or (jobs[job_id].get("user_gate") or {}).get("lifecycle") == "REQUIRED"
                or job_patch.get("decision", {}).get("needs_user") is not False
                or event_update is not None or user_gate_update is not None
                or adopt_protocol or protocol_migrations is not None
                or project_contract_request is not None or new_jobs_request is not None
                or new_job_request is not None or queue_event_update is not None):
            raise ControlError("MAINTENANCE_RESOLUTION_SCOPE_INVALID")
        evidence = maintenance_resolution.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ControlError("MAINTENANCE_RESOLUTION_EVIDENCE_REQUIRED")
        for item in evidence:
            evidence_path = (root / item["path"]).resolve()
            if (not evidence_path.is_relative_to((root / "control" / "diagnostics").resolve())
                    or not evidence_path.is_file()
                    or digest_file(evidence_path) != item.get("sha256")):
                raise ControlError("MAINTENANCE_RESOLUTION_EVIDENCE_INVALID")
    reconcile_queue_authority = bool(request.get("reconcile_queue_authority"))
    if new_job_request is not None and new_jobs_request is not None:
        raise ControlError("new_job and new_jobs are mutually exclusive")
    if adopt_protocol and protocol_migrations is not None:
        raise ControlError("adopt_protocol_contract and protocol_migrations are mutually exclusive")
    if request.get("retain_lease") and not (
        role == "SOL"
        and lease.get("planned_action", {}).get("type") in {"SOL_HANDLE_EVENT", "SOL_HANDLE_QUEUE_EMPTY"}
        and handler_checkpoint
    ):
        raise ControlError("retain_lease is allowed only for a Sol event acknowledgement checkpoint")
    if reconcile_queue_authority and role not in {"USER", "MIGRATION"}:
        raise ControlError("queue authority reconciliation is allowed only for USER or MIGRATION")
    if project_contract_request is not None:
        if role not in {"MIGRATION", "USER"}:
            raise ControlError("only USER or MIGRATION may replace the project contract")
        if not isinstance(project_contract_request, dict):
            raise ControlError("project_contract must be a complete object")
        current_project_hash = digest_file(control_paths(root)["project_contract"])
        superseded = set(project_contract_request.get("supersedes_contract_sha256", []))
        if current_project_hash not in superseded:
            raise ControlError("new project contract must record the current contract hash as superseded")
        if not project_contract_request.get("authorization", {}).get("basis"):
            raise ControlError("new project contract must record its user authorization basis")
    if role not in {"MIGRATION", "USER"} and any(key in runtime_patch for key in ("schema_version", "controller", "defaults")):
        raise ControlError("this role cannot change runtime policy")
    if role not in {"MIGRATION", "USER"} and any(key in queue_patch for key in ("schema_version", "queue_revision", "planning_generation")):
        raise ControlError("this role cannot directly change queue control fields")
    if job_patch:
        if not job_id or job_id not in jobs:
            raise ControlError("job patch requires an existing job_id")
        protected = {"schema_version", "job_id", "workspace", "baseline", "runtime_tracking", "user_gate"}
        if role not in {"MIGRATION", "USER"} and protected.intersection(job_patch):
            raise ControlError("job patch changes protected identity/baseline fields")
        controller_patch = job_patch.get("controller", {})
        if any(
            key in controller_patch
            for key in (
                "state_revision",
                "semantic_revision",
                "event_generation",
                "current_event",
                "event_id",
                "last_handled_event_id",
                "protocol_contract_sha256",
            )
        ):
            raise ControlError("EVENT_IDENTITY_FIELDS_CONTROLLER_OWNED")
        if set(controller_patch) - {"escalation_metrics"}:
            raise ControlError("job.controller patch may contain only escalation_metrics facts")
        if "authorized_specification" in job_patch and role != "SOL":
            raise ControlError("only Sol may authorize a technical protocol revision")
    if event_update is not None and not isinstance(event_update, dict):
        raise ControlError("event_update must be an object")
    if user_gate_update is not None and not isinstance(user_gate_update, dict):
        raise ControlError("user_gate_update must be an object")
    if queue_event_update is not None and not isinstance(queue_event_update, dict):
        raise ControlError("queue_event_update must be an object")
    if "last_empty_event" in queue_patch:
        raise ControlError("PROJECT_EVENT_IDENTITY_FIELDS_CONTROLLER_OWNED")
    if new_job_request is not None:
        if role != "SOL" or lease["planned_action"]["type"] != "SOL_HANDLE_QUEUE_EMPTY":
            raise ControlError("new_job is allowed only for the bound queue-empty Sol event")
        if not isinstance(new_job_request, dict) or not isinstance(new_job_request.get("state"), dict):
            raise ControlError("new_job requires a complete state object")
        new_job_id = new_job_request.get("job_id")
        if new_job_id in jobs or (root / "jobs" / str(new_job_id) / "state.json").exists():
            raise ControlError("new_job already exists")
        # specification.md and protocol-contract.json are prepared by Sol;
        # state.json becomes live only through this transaction.
        if not specification_path(root, new_job_id).exists() or not protocol_contract_path(root, new_job_id).exists():
            raise ControlError("new_job specification and protocol contract must exist before commit")
        job_id = new_job_id
    if new_jobs_request is not None:
        if role not in {"MIGRATION", "USER"}:
            raise ControlError("new_jobs is allowed only for USER or MIGRATION")
        if runtime.get("active_job_id") is not None or queue["jobs"]:
            raise ControlError("batch queue seeding requires no active job and an empty queue")
        if not isinstance(new_jobs_request, list) or not new_jobs_request:
            raise ControlError("new_jobs must be a non-empty list")
        requested_ids: list[str] = []
        for item in new_jobs_request:
            if not isinstance(item, dict) or not isinstance(item.get("state"), dict):
                raise ControlError("every new_jobs entry requires a complete state object")
            new_job_id = item.get("job_id")
            state_path(root, new_job_id)
            if new_job_id in requested_ids or new_job_id in jobs or (root / "jobs" / str(new_job_id) / "state.json").exists():
                raise ControlError(f"new job already exists: {new_job_id}")
            if not specification_path(root, new_job_id).exists() or not protocol_contract_path(root, new_job_id).exists():
                raise ControlError(f"new job {new_job_id} specification and protocol contract must exist before commit")
            requested_ids.append(new_job_id)
        queued_ids = [entry.get("job_id") for entry in queue_patch.get("jobs", [])]
        if queued_ids != requested_ids:
            raise ControlError("batch queue entries must exactly match new_jobs order")
    migration_contracts: dict[str, dict[str, Any]] = {}
    if protocol_migrations is not None:
        if role not in {"MIGRATION", "USER"}:
            raise ControlError("protocol_migrations is allowed only for USER or MIGRATION")
        if not isinstance(protocol_migrations, list) or not protocol_migrations:
            raise ControlError("protocol_migrations must be a non-empty list")
        if "jobs" in queue_patch:
            raise ControlError("protocol_migrations owns queue job hashes")
        requested_ids: list[str] = []
        queued_ids = {entry["job_id"] for entry in queue["jobs"]}
        project_hash = runtime["controller"]["project_contract_sha256"]
        for item in protocol_migrations:
            if not isinstance(item, dict) or not item.get("job_id"):
                raise ControlError("every protocol migration requires job_id")
            migration_job_id = item["job_id"]
            if migration_job_id in requested_ids:
                raise ControlError(f"duplicate protocol migration: {migration_job_id}")
            requested_ids.append(migration_job_id)
            if migration_job_id not in jobs or migration_job_id not in queued_ids:
                raise ControlError(f"protocol migration job is not queued: {migration_job_id}")
            old_job = jobs[migration_job_id]
            if old_job["status"] != "QUEUED":
                raise ControlError(f"protocol migration requires QUEUED status: {migration_job_id}")
            old_revision = int(old_job["authorized_specification"]["protocol_revision"])
            history_root = root / "jobs" / migration_job_id / "history" / f"protocol-revision-{old_revision}"
            history_spec = history_root / "specification.md"
            history_contract = history_root / "protocol-contract.json"
            if not history_spec.exists() or not history_contract.exists():
                raise ControlError(f"protocol migration history is incomplete: {migration_job_id}")
            if digest_file(history_spec) != old_job["authorized_specification"]["current_sha256"]:
                raise ControlError(f"preserved specification hash mismatch: {migration_job_id}")
            if digest_file(history_contract) != old_job["controller"]["protocol_contract_sha256"]:
                raise ControlError(f"preserved protocol contract hash mismatch: {migration_job_id}")
            contract_path = protocol_contract_path(root, migration_job_id)
            contract = read_json(contract_path)
            if contract.get("job_id") != migration_job_id:
                raise ControlError(f"protocol migration contract job mismatch: {migration_job_id}")
            if contract.get("project_contract_sha256") != project_hash:
                raise ControlError(f"protocol migration changes project contract: {migration_job_id}")
            if contract.get("specification_sha256") != digest_file(specification_path(root, migration_job_id)):
                raise ControlError(f"protocol migration specification hash mismatch: {migration_job_id}")
            if contract.get("protocol_revision") != old_revision + 1:
                raise ControlError(f"protocol migration revision must advance by one: {migration_job_id}")
            preserved = contract.get("preserved_protocol_revisions") or []
            expected_history = {
                "protocol_revision": old_revision,
                "specification": relative(root, history_spec),
                "specification_sha256": digest_file(history_spec),
                "protocol_contract": relative(root, history_contract),
                "protocol_contract_sha256": digest_file(history_contract),
            }
            if expected_history not in preserved:
                raise ControlError(f"protocol migration does not preserve the prior revision: {migration_job_id}")
            if not contract.get("authorized_at") or not contract.get("authorized_by_event_id"):
                raise ControlError(f"protocol migration authority metadata is incomplete: {migration_job_id}")
            migration_contracts[migration_job_id] = contract

    updated = copy.deepcopy(current)
    runtime_rel = "control/runtime.json"
    queue_rel = "control/queue.json"
    project_rel = "control/project-contract.json"
    planned_type = lease["planned_action"]["type"]
    if planned_type == "HANDOFF_ACCEPTED_JOB" and role not in {"USER", "MIGRATION"}:
        if any((runtime_patch, queue_patch, job_patch, event_update, user_gate_update, queue_event_update, new_job_request, new_jobs_request, project_contract_request, protocol_migrations)):
            raise ControlError("HANDOFF_ACCEPTED_JOB is controller-owned and rejects caller patches")
        updated = apply_handoff_accepted_job(root, current, runtime, queue, jobs, job_id)
    elif planned_type == "COMPLETE_PROJECT" and role not in {"USER", "MIGRATION"}:
        if any((runtime_patch, queue_patch, job_patch, event_update, user_gate_update, queue_event_update)):
            raise ControlError("COMPLETE_PROJECT is controller-owned and rejects caller patches")
        completion = validate_project_completion(root, runtime, queue, jobs)
        if not completion["ok"]:
            raise ControlError("PROJECT_COMPLETION_NOT_CLOSED")
        updated[runtime_rel]["project_status"] = "COMPLETED"
    else:
        updated[runtime_rel] = deep_merge(updated[runtime_rel], runtime_patch)
    if queue_event_update:
        queue_checkpoint = (role == "SOL" and planned_type == "SOL_HANDLE_QUEUE_EMPTY"
                            and queue_event_update == {"lifecycle": "ACKNOWLEDGED"} and request.get("retain_lease"))
        if role != "LUNA" and not queue_checkpoint:
            raise ControlError("PROJECT_EVENT_UPDATE_NOT_AUTHORIZED")
        event_id = lease["planned_action"].get("event_id")
        recovering_queue_dispatch = (planned_type == "RECOVER_INTENT"
                                     and lease["planned_action"]["intent"]["action"]["type"] == "SOL_QUEUE_EMPTY")
        recovering_queue_status = (planned_type == "RECOVER_INTENT"
                                   and lease["planned_action"]["intent"]["action"]["type"] == "SOL_STATUS")
        if recovering_queue_dispatch or recovering_queue_status:
            event_id = lease["planned_action"]["intent"]["action"]["event_id"]
        if (planned_type == "SOL_QUEUE_EMPTY" or recovering_queue_dispatch) and queue_event_update.get("lifecycle") == "DISPATCHED":
            thread_id = queue_event_update.get("thread_id")
            if not isinstance(thread_id, str) or not thread_id.strip():
                raise ControlError("SOL_DISPATCH_REQUIRES_THREAD_ID")
            if capability_required:
                from control.exception_handler import validate_dispatch_receipt
                validate_dispatch_receipt(root, request.get("dispatch_receipt"), event_id, thread_id,
                                          (request.get("resolve_intent") or {}).get("intent_id"))
            updated[queue_rel]["last_empty_event"] = {
                "event_id": event_id,
                "status": "DISPATCHED",
                "thread_id": thread_id,
                "dispatched_at": queue_event_update.get("dispatched_at") or iso_utc(),
                **({"dispatch_receipt": request["dispatch_receipt"]} if capability_required else {}),
            }
        elif queue_checkpoint:
            current_queue_event = copy.deepcopy(updated[queue_rel].get("last_empty_event") or {})
            if (current_queue_event.get("event_id") != event_id or current_queue_event.get("status") != "DISPATCHED"
                    or current_queue_event.get("thread_id") != lease.get("source_thread_id")):
                raise ControlError("PROJECT_EVENT_ACK_IDENTITY_MISMATCH")
            current_queue_event["acknowledged_at"] = current_queue_event.get("acknowledged_at") or iso_utc()
            current_queue_event["capability_receipt"] = capability_reference
            updated[queue_rel]["last_empty_event"] = current_queue_event
        elif planned_type == "SOL_STATUS" or recovering_queue_status:
            current_queue_event = copy.deepcopy(updated[queue_rel].get("last_empty_event") or {})
            if current_queue_event.get("event_id") != event_id or current_queue_event.get("status") != "DISPATCHED":
                raise ControlError("PROJECT_EVENT_STATUS_IDENTITY_MISMATCH")
            allowed = {"thread_status", "status_checked_at", "recovery_requested_at", "recovery_outcome"}
            if set(queue_event_update) - allowed:
                raise ControlError("PROJECT_EVENT_STATUS_FIELDS_INVALID")
            current_queue_event.update(copy.deepcopy(queue_event_update))
            if capability_required and queue_event_update.get("recovery_requested_at"):
                current_queue_event["dispatch_receipt"] = request["dispatch_receipt"]
            current_queue_event["status_checked_at"] = queue_event_update.get("status_checked_at") or iso_utc()
            updated[queue_rel]["last_empty_event"] = current_queue_event
        else:
            raise ControlError("PROJECT_EVENT_UPDATE_NOT_AUTHORIZED")
        updated[queue_rel]["updated_at"] = iso_utc()
    if project_contract_request is not None:
        updated[project_rel] = copy.deepcopy(project_contract_request)
        updated[runtime_rel].setdefault("controller", {})["project_contract_sha256"] = digest_persisted_json(
            project_contract_request
        )
    if queue_patch:
        before_jobs = queue["jobs"]
        updated[queue_rel] = deep_merge(updated[queue_rel], queue_patch)
        if updated[queue_rel]["jobs"] != before_jobs:
            updated[queue_rel]["queue_revision"] = queue["queue_revision"] + 1
        updated[queue_rel]["updated_at"] = iso_utc()

    if reconcile_queue_authority:
        changed = False
        for entry in updated[queue_rel]["jobs"]:
            queued_job_id = entry["job_id"]
            specification_hash = digest_file(specification_path(root, queued_job_id))
            contract_hash = digest_file(protocol_contract_path(root, queued_job_id))
            if (
                entry.get("specification_sha256") != specification_hash
                or entry.get("protocol_contract_sha256") != contract_hash
            ):
                entry["specification_sha256"] = specification_hash
                entry["protocol_contract_sha256"] = contract_hash
                changed = True
        if changed:
            updated[queue_rel]["queue_revision"] = int(updated[queue_rel]["queue_revision"]) + 1
            updated[queue_rel]["updated_at"] = iso_utc()

    if new_job_request is not None:
        new_job = copy.deepcopy(new_job_request["state"])
        new_job_id = new_job_request["job_id"]
        new_job["schema_version"] = JOB_SCHEMA
        new_job["job_id"] = new_job_id
        new_job["status"] = "QUEUED"
        new_job["updated_at"] = iso_utc()
        new_job.setdefault("controller", {})["state_revision"] = 1
        new_job["controller"]["semantic_revision"] = 1
        new_job["controller"]["event_generation"] = 0
        new_job["controller"]["current_event"] = None
        new_job["controller"].pop("event_id", None)
        new_job["controller"]["protocol_contract_sha256"] = digest_file(protocol_contract_path(root, new_job_id))
        new_job.setdefault("user_gate", None)
        new_job["controller"].setdefault(
            "escalation_metrics",
            {"total_dispatches": 0, "current_root_cause_signature": None, "root_causes": {}},
        )
        updated[f"jobs/{new_job_id}/state.json"] = new_job
        updated[queue_rel]["planning_generation"] = queue["planning_generation"] + 1
        updated[queue_rel]["updated_at"] = iso_utc()

    if new_jobs_request is not None:
        for item in new_jobs_request:
            new_job = copy.deepcopy(item["state"])
            new_job_id = item["job_id"]
            new_job["schema_version"] = JOB_SCHEMA
            new_job["job_id"] = new_job_id
            new_job["status"] = "QUEUED"
            new_job["updated_at"] = iso_utc()
            new_job.setdefault("controller", {})["state_revision"] = 1
            new_job["controller"]["semantic_revision"] = 1
            new_job["controller"]["event_generation"] = 0
            new_job["controller"]["current_event"] = None
            new_job["controller"].pop("event_id", None)
            new_job["controller"]["protocol_contract_sha256"] = digest_file(protocol_contract_path(root, new_job_id))
            new_job.setdefault("user_gate", None)
            new_job["controller"].setdefault(
                "escalation_metrics",
                {"total_dispatches": 0, "current_root_cause_signature": None, "root_causes": {}},
            )
            updated[f"jobs/{new_job_id}/state.json"] = new_job
        updated[queue_rel]["planning_generation"] = queue["planning_generation"] + 1
        updated[queue_rel]["updated_at"] = iso_utc()

    if migration_contracts:
        queue_entries = copy.deepcopy(updated[queue_rel]["jobs"])
        entries_by_id = {entry["job_id"]: entry for entry in queue_entries}
        migrated_at = iso_utc()
        for migration_job_id, contract in migration_contracts.items():
            old_job = jobs[migration_job_id]
            new_job = copy.deepcopy(old_job)
            authorized = new_job["authorized_specification"]
            authorized.update(
                {
                    "current_sha256": contract["specification_sha256"],
                    "authorized_at": contract["authorized_at"],
                    "authorized_by_event_id": contract["authorized_by_event_id"],
                    "protocol_revision": contract["protocol_revision"],
                    "protocol_authority_sha256": contract["protocol_authority_sha256"],
                    "canonical_replay_a": contract.get("canonical_replay_a"),
                    "canonical_replay_b": contract.get("canonical_replay_b"),
                    "canonical_formal": contract.get("canonical_formal"),
                }
            )
            contract_hash = digest_file(protocol_contract_path(root, migration_job_id))
            new_job["controller"]["protocol_contract_sha256"] = contract_hash
            new_job["updated_at"] = migrated_at
            finalize_job_revisions(old_job, new_job)
            updated[f"jobs/{migration_job_id}/state.json"] = new_job
            queue_entry = entries_by_id[migration_job_id]
            queue_entry["specification_sha256"] = contract["specification_sha256"]
            queue_entry["protocol_contract_sha256"] = contract_hash
        updated[queue_rel]["jobs"] = queue_entries
        updated[queue_rel]["queue_revision"] = int(updated[queue_rel]["queue_revision"]) + 1
        updated[queue_rel]["planning_generation"] = int(updated[queue_rel]["planning_generation"]) + 1
        updated[queue_rel]["updated_at"] = migrated_at

    if job_patch or event_update or user_gate_update or handler_retry:
        job_rel = f"jobs/{job_id}/state.json"
        old_job = jobs[job_id]
        new_job = deep_merge(old_job, job_patch)
        old_status = old_job["status"]
        new_status = new_job["status"]
        if new_status not in VALID_TRANSITIONS[old_status]:
            raise ControlError(f"invalid state transition {old_status} -> {new_status}")
        old_session = old_job["dsh"].get("session_id")
        new_session = new_job["dsh"].get("session_id")
        if old_session and new_session != old_session:
            raise ControlError("an existing DSH session cannot be replaced")
        planned = lease["planned_action"]
        old_event = old_job["controller"].get("current_event")
        current_event = copy.deepcopy(new_job["controller"].get("current_event"))
        if handler_retry:
            current_event["user_retry_requested_at"] = iso_utc()
            current_event["user_retry_authorization"] = copy.deepcopy(handler_retry)
            new_job["controller"]["current_event"] = current_event

        if user_gate_update:
            if role not in {"USER", "MIGRATION"}:
                raise ControlError("USER_GATE_UPDATE_NOT_AUTHORIZED")
            gate = copy.deepcopy(old_job.get("user_gate") or {})
            if gate.get("lifecycle") != "REQUIRED":
                raise ControlError("USER_GATE_IS_NOT_OPEN")
            if user_gate_update.get("gate_id") != gate.get("gate_id"):
                raise ControlError("USER_GATE_ID_MISMATCH")
            lifecycle = user_gate_update.get("lifecycle")
            if lifecycle not in {"AUTHORIZED", "REFUSED", "EXPIRED"}:
                raise ControlError("USER_GATE_UPDATE_INVALID")
            gate["lifecycle"] = lifecycle
            gate["resolved_at"] = iso_utc()
            new_job["user_gate"] = gate
            new_job["decision"]["needs_user"] = False

        if event_update:
            lifecycle = event_update.get("lifecycle")
            if lifecycle == "DISPATCHED":
                dispatch_action = planned
                recovering_dispatch = (
                    role in {"LUNA", "USER", "MIGRATION"}
                    and planned.get("type") == "RECOVER_INTENT"
                    and planned.get("intent", {}).get("action", {}).get("type") == "SOL_ESCALATE"
                )
                if recovering_dispatch:
                    intent = planned["intent"]
                    resolution = request.get("resolve_intent") or {}
                    dispatch_action = intent["action"]
                    if (
                        resolution.get("intent_id") != intent["intent_id"]
                        or not event_update.get("thread_id")
                        or resolution.get("external_id") != event_update.get("thread_id")
                        or dispatch_action.get("job_id") != job_id
                        or not resolution.get("summary")
                    ):
                        raise ControlError("EVENT_RECOVERY_IDENTITY_MISMATCH")
                elif role != "LUNA" or planned.get("type") != "SOL_ESCALATE":
                    raise ControlError("EVENT_DISPATCH_NOT_AUTHORIZED")
                if not current_event or current_event.get("event_id") != dispatch_action.get("event_id"):
                    raise ControlError("EVENT_DISPATCH_ID_MISMATCH")
                if current_event.get("lifecycle") != "REQUIRED":
                    raise ControlError("EVENT_DISPATCH_LIFECYCLE_INVALID")
                thread_id = event_update.get("thread_id")
                if not isinstance(thread_id, str) or not thread_id.strip():
                    raise ControlError("SOL_DISPATCH_REQUIRES_THREAD_ID")
                if capability_required:
                    from control.exception_handler import validate_dispatch_receipt
                    validate_dispatch_receipt(root, request.get("dispatch_receipt"), current_event["event_id"], thread_id,
                                              (request.get("resolve_intent") or {}).get("intent_id"))
                    current_event["dispatch_receipt"] = request["dispatch_receipt"]
                current_event["lifecycle"] = "DISPATCHED"
                current_event["thread_id"] = thread_id
                current_event["dispatched_at"] = event_update.get("dispatched_at") or iso_utc()
                new_job["controller"]["current_event"] = current_event
            elif lifecycle == "ACKNOWLEDGED":
                if role != "SOL" or planned.get("type") != "SOL_HANDLE_EVENT":
                    raise ControlError("EVENT_ACKNOWLEDGEMENT_NOT_AUTHORIZED")
                if not request.get("retain_lease"):
                    raise ControlError("EVENT_ACKNOWLEDGEMENT_MUST_RETAIN_LEASE")
                if not current_event or current_event.get("event_id") != planned.get("event_id"):
                    raise ControlError("EVENT_ACKNOWLEDGEMENT_ID_MISMATCH")
                if current_event.get("lifecycle") not in {"DISPATCHED", "ACKNOWLEDGED"}:
                    raise ControlError("EVENT_ACKNOWLEDGEMENT_LIFECYCLE_INVALID")
                current_event["lifecycle"] = "ACKNOWLEDGED"
                current_event["acknowledged_at"] = current_event.get("acknowledged_at") or iso_utc()
                current_event["acknowledged_by_thread_id"] = lease.get("source_thread_id")
                if capability_reference:
                    current_event["capability_receipt"] = capability_reference
                new_job["controller"]["current_event"] = current_event
            elif role == "LUNA" and (planned.get("type") == "SOL_STATUS" or (
                    planned.get("type") == "RECOVER_INTENT" and planned["intent"]["action"]["type"] == "SOL_STATUS")):
                observed_action = (planned.get("intent") or {}).get("action") or planned
                if not current_event or current_event.get("event_id") != observed_action.get("event_id"):
                    raise ControlError("EVENT_STATUS_IDENTITY_MISMATCH")
                if current_event.get("lifecycle") not in {"DISPATCHED", "ACKNOWLEDGED"}:
                    raise ControlError("EVENT_STATUS_LIFECYCLE_INVALID")
                allowed = {"thread_status", "status_checked_at", "recovery_requested_at", "recovery_outcome"}
                if set(event_update) - allowed:
                    raise ControlError("EVENT_STATUS_FIELDS_INVALID")
                current_event.update(copy.deepcopy(event_update))
                if capability_required and event_update.get("recovery_requested_at"):
                    current_event["dispatch_receipt"] = request["dispatch_receipt"]
                current_event["status_checked_at"] = event_update.get("status_checked_at") or iso_utc()
                new_job["controller"]["current_event"] = current_event
            elif lifecycle not in {None, "RESOLVED"}:
                raise ControlError("EVENT_LIFECYCLE_UPDATE_NOT_AUTHORIZED")

        handling_open_event = (
            role == "SOL"
            and planned.get("type") == "SOL_HANDLE_EVENT"
            and isinstance(old_event, dict)
            and old_event.get("lifecycle") in {"DISPATCHED", "ACKNOWLEDGED"}
            and old_event.get("event_id") == planned.get("event_id")
        )
        claim_only = event_update is not None and event_update.get("lifecycle") == "ACKNOWLEDGED"
        semantic_outcome = (
            new_status != old_status
            or new_job.get("decision", {}).get("needs_user")
            or new_job.get("decision", {}).get("route") == "TERMINAL_TECHNICAL_FAILURE"
        )
        resolving_now = handling_open_event and semantic_outcome and not claim_only
        if resolving_now:
            current_event = copy.deepcopy(new_job["controller"].get("current_event") or old_event)
            if not current_event.get("acknowledged_at"):
                current_event["acknowledged_at"] = lease["started_at"]
                current_event["acknowledged_by_thread_id"] = lease.get("source_thread_id")
            metrics = new_job["controller"]["escalation_metrics"]
            signature = current_event.get("root_cause_signature") or "UNCLASSIFIED"
            roots = metrics.setdefault("root_causes", {})
            total_exhausted = int(metrics.get("total_dispatches", 0)) >= int(runtime["defaults"]["max_sol_escalations_per_job"])
            root_exhausted = int(roots.get(signature, 0)) >= int(runtime["defaults"]["max_same_root_cause_escalations"])
            terminal = new_status == "FAILED" and new_job.get("decision", {}).get("route") == "TERMINAL_TECHNICAL_FAILURE"
            if (total_exhausted or root_exhausted) and terminal:
                current_event["budget_disposition"] = "EXHAUSTED_TERMINAL_NO_ADDITIONAL_CHARGE"
            else:
                if total_exhausted:
                    raise ControlError("Sol escalation budget would be exceeded")
                if root_exhausted:
                    raise ControlError("same-root-cause escalation budget would be exceeded")
                metrics["total_dispatches"] = int(metrics.get("total_dispatches", 0)) + 1
                roots[signature] = int(roots.get(signature, 0)) + 1

        if old_status == "REVIEW_PENDING" and new_status == "ACCEPTED":
            if role != "LUNA":
                raise ControlError("ROLE_NOT_AUTHORIZED_FOR_ACCEPTANCE")
            if planned.get("type") != "REVIEW":
                raise ControlError("ACCEPTANCE_REQUIRES_REVIEW_ACTION")
            validate_v81_acceptance(new_job)

        tracking = new_job["runtime_tracking"]
        now = utc_now()
        if old_status != "RUNNING" and new_status == "RUNNING" and tracking.get("started_at") is None:
            tracking["started_at"] = iso_utc(now)
        if old_status == "RUNNING" and new_status != "RUNNING" and tracking.get("started_at"):
            elapsed = max(0.0, (now - parse_time(tracking["started_at"])).total_seconds())
            tracking["accumulated_seconds"] = round(float(tracking.get("accumulated_seconds", 0)) + elapsed, 3)
            tracking["started_at"] = None
        old_summary = old_job["dsh"].get("last_result_summary")
        new_summary = new_job["dsh"].get("last_result_summary")
        if new_summary and new_summary != old_summary:
            tracking["last_progress_at"] = iso_utc(now)
            tracking["progress_fingerprint"] = digest_bytes(new_summary.encode("utf-8"))

        if new_job.get("decision", {}).get("needs_user"):
            if role != "SOL" or planned.get("type") != "SOL_HANDLE_EVENT":
                raise ControlError("USER_GATE_REQUIRES_BOUND_SOL_EVENT")
            new_job["user_gate"] = make_user_gate(new_job)

        if handling_open_event and not claim_only and not semantic_outcome:
            raise ControlError("EVENT_RESOLUTION_WITHOUT_SEMANTIC_OUTCOME")
        if resolving_now:
            current_event = copy.deepcopy(current_event)
            current_event["lifecycle"] = "RESOLVED"
            current_event["resolved_at"] = iso_utc(now)
            current_event["resolution"] = (
                "USER_REQUIRED" if new_job.get("decision", {}).get("needs_user") else new_job.get("status")
            )
            new_job["controller"]["current_event"] = current_event
            new_job["controller"]["last_handled_event_id"] = current_event["event_id"]

        if maintenance_resolution is not None:
            current_event = copy.deepcopy(old_event)
            current_event.update({
                "lifecycle": "RESOLVED", "resolved_at": iso_utc(now),
                "resolution": "USER_MAINTENANCE_REQUEUED",
                "maintenance_receipt": copy.deepcopy(maintenance_resolution),
                "budget_disposition": "USER_MAINTENANCE_NO_AUTOMATIC_ESCALATION",
            })
            new_job["controller"]["current_event"] = current_event
            new_job["controller"]["last_handled_event_id"] = current_event["event_id"]
        ensure_technical_event(old_job, new_job)
        new_job["updated_at"] = iso_utc()
        finalize_job_revisions(old_job, new_job)
        updated[job_rel] = new_job

    if adopt_protocol:
        if role != "SOL":
            raise ControlError("only Sol may adopt a protocol contract")
        if not job_id:
            raise ControlError("protocol adoption requires job_id")
        contract_path = protocol_contract_path(root, job_id)
        specification_target = f"jobs/{job_id}/specification.md"
        contract_target = f"jobs/{job_id}/protocol-contract.json"
        if protocol_file_updates:
            try:
                specification_bytes = base64.b64decode(protocol_file_updates[specification_target]["data"], validate=True)
                contract_bytes = base64.b64decode(protocol_file_updates[contract_target]["data"], validate=True)
                contract = json.loads(contract_bytes.decode("utf-8-sig"))
            except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ControlError("staged protocol files are incomplete or invalid") from exc
            if not isinstance(contract, dict):
                raise ControlError("staged protocol contract must be a JSON object")
            contract_hash = digest_bytes(contract_bytes)
            specification_hash = digest_bytes(specification_bytes)
        else:
            # Compatibility for historical direct-file adoption requests. New
            # Sol prompts always use protocol_staging so the files participate
            # in transaction recovery.
            contract = read_json(contract_path)
            contract_hash = digest_file(contract_path)
            specification_hash = digest_file(specification_path(root, job_id))
        project_hash = updated[runtime_rel]["controller"]["project_contract_sha256"]
        if contract.get("job_id") != job_id:
            raise ControlError("protocol contract job mismatch")
        if contract.get("project_contract_sha256") != project_hash:
            raise ControlError("protocol contract changes the project contract")
        if contract.get("specification_sha256") != specification_hash:
            raise ControlError("protocol contract does not match specification")
        old_job = jobs[job_id]
        old_revision = int(old_job["authorized_specification"]["protocol_revision"])
        if contract.get("protocol_revision") != old_revision + 1:
            raise ControlError("protocol revision must advance by one")
        if contract.get("authorized_by_event_id") != lease["planned_action"].get("event_id"):
            raise ControlError("protocol revision authority must be the bound Sol event")
        history_root = root / "jobs" / job_id / "history" / f"protocol-revision-{old_revision}"
        history_spec = history_root / "specification.md"
        history_contract = history_root / "protocol-contract.json"
        if not history_spec.exists() or not history_contract.exists():
            raise ControlError("protocol revision history is incomplete")
        if digest_file(history_spec) != old_job["authorized_specification"]["current_sha256"]:
            raise ControlError("preserved specification hash mismatch")
        if digest_file(history_contract) != old_job["controller"]["protocol_contract_sha256"]:
            raise ControlError("preserved protocol contract hash mismatch")
        expected_history = {
            "protocol_revision": old_revision,
            "specification": relative(root, history_spec),
            "specification_sha256": digest_file(history_spec),
            "protocol_contract": relative(root, history_contract),
            "protocol_contract_sha256": digest_file(history_contract),
        }
        if expected_history not in (contract.get("preserved_protocol_revisions") or []):
            raise ControlError("protocol revision does not preserve the prior revision")
        job_rel = f"jobs/{job_id}/state.json"
        new_job = updated[job_rel]
        authorized = new_job["authorized_specification"]
        for state_key, contract_key in {
            "current_sha256": "specification_sha256",
            "protocol_revision": "protocol_revision",
            "protocol_authority_sha256": "protocol_authority_sha256",
            "canonical_replay_a": "canonical_replay_a",
            "canonical_replay_b": "canonical_replay_b",
            "canonical_formal": "canonical_formal",
        }.items():
            authorized[state_key] = contract.get(contract_key)
        authorized["authorized_at"] = contract.get("authorized_at")
        authorized["authorized_by_event_id"] = contract.get("authorized_by_event_id")
        new_job["controller"]["protocol_contract_sha256"] = contract_hash
        finalize_job_revisions(old_job, new_job)
        for entry in updated[queue_rel]["jobs"]:
            if entry.get("job_id") == job_id:
                entry["specification_sha256"] = specification_hash
                entry["protocol_contract_sha256"] = contract_hash
                updated[queue_rel]["queue_revision"] = int(updated[queue_rel]["queue_revision"]) + 1
                updated[queue_rel]["updated_at"] = iso_utc()
                break

    finish = request.get("finish")
    if not isinstance(finish, dict) or not finish.get("outcome"):
        raise ControlError("every commit must finish the run with an outcome")
    finished = utc_now()
    started = parse_time(lease["started_at"])
    observed_duration = round((finished - started).total_seconds(), 3)
    interval = int(updated[runtime_rel]["defaults"]["check_interval_minutes"])
    updated[runtime_rel]["updated_at"] = iso_utc(finished)
    updated[runtime_rel]["next_expected_run_at"] = iso_utc(finished + timedelta(minutes=interval))
    updated[runtime_rel]["last_supervisor_run"] = {
        "started_at": lease["started_at"],
        "finished_at": iso_utc(finished),
        "observed_duration_seconds": observed_duration,
        "source_thread_id": lease.get("source_thread_id"),
        "role": role,
        "outcome": finish["outcome"],
        "job_id": job_id,
        "action": lease["planned_action"]["type"],
    }
    planned = lease["planned_action"]
    policy_violations: list[str] = []
    routing_wall_limit = updated[runtime_rel]["defaults"].get("max_routing_heartbeat_wall_seconds")
    if (
        role == "LUNA"
        and planned.get("type") in ROUTING_ACTIONS
        and routing_wall_limit is not None
        and observed_duration > float(routing_wall_limit)
    ):
        policy_violations.append("ROUTING_HEARTBEAT_WALL_EXCEEDED")
    if policy_violations:
        updated[runtime_rel]["last_supervisor_run"]["policy_violations"] = policy_violations
    if role == "SOL" and planned.get("type") == "SOL_HANDLE_EVENT" and job_id:
        handled_job = updated[f"jobs/{job_id}/state.json"]
        handled_escalation = handled_job["controller"].get("current_event") or {}
        if (
            handled_escalation.get("event_id") == planned.get("event_id")
            and handled_escalation.get("lifecycle") == "RESOLVED"
        ):
            updated[runtime_rel]["last_sol_event"] = {
                "event_id": planned["event_id"],
                "thread_id": handled_escalation.get("thread_id") or lease.get("source_thread_id"),
                "finished_at": iso_utc(finished),
                "outcome": finish["outcome"],
                "job_id": job_id,
                "next_status": handled_job["status"],
                "result_summary": (
                    (handled_job.get("result") or {}).get("summary")
                    or finish.get("summary")
                    or request.get("reason")
                ),
            }

    resolution: dict[str, Any] | None = None
    resolve_request = request.get("resolve_intent")
    if resolve_request is not None:
        intent_id = resolve_request.get("intent_id")
        intent_path = control_paths(root)["intents"] / f"{intent_id}.json"
        intent = read_json(intent_path)
        if (control_paths(root)["intents"] / f"{intent_id}.resolved.json").exists():
            raise ControlError("intent is already resolved")
        resolution = {
            "schema_version": 1,
            "intent_id": intent_id,
            "correlation_id": intent["correlation_id"],
            "resolved_at": iso_utc(finished),
            "outcome": resolve_request.get("outcome"),
            "external_id": resolve_request.get("external_id"),
            "summary": resolve_request.get("summary"),
        }

    # QUEUE_EMPTY cannot be resolved without a concrete postcondition.
    planned = lease["planned_action"]
    if planned["type"] == "SOL_HANDLE_QUEUE_EMPTY" and not (capability_failure or handler_checkpoint):
        queue_after = updated[queue_rel]
        runtime_after = updated[runtime_rel]
        if not queue_after["jobs"] and runtime_after["project_status"] != "COMPLETED":
            raise ControlError("QUEUE_EMPTY may resolve only by adding a job or completing the project")
        queue_event = queue_after.get("last_empty_event") or {}
        if queue_event.get("event_id") != planned.get("event_id") or queue_event.get("status") != "DISPATCHED":
            raise ControlError("QUEUE_EMPTY_EVENT_IDENTITY_MISMATCH")
        queue_event["status"] = "RESOLVED"
        queue_event["resolved_at"] = iso_utc(finished)
        queue_event["result_summary"] = finish.get("summary") or request.get("reason")
        queue_after["last_empty_event"] = queue_event
        queue_after["updated_at"] = iso_utc(finished)

    runtime_after, queue_after, jobs_after = validate_all(root, updated, check_contracts=not bool(protocol_file_updates))
    if runtime["project_status"] != "COMPLETED" and runtime_after["project_status"] == "COMPLETED":
        completion = validate_project_completion(root, runtime_after, queue_after, jobs_after)
        # The completion validator requires ACTIVE immediately before closure.
        completion_runtime = copy.deepcopy(runtime_after)
        completion_runtime["project_status"] = "ACTIVE"
        completion = validate_project_completion(root, completion_runtime, queue_after, jobs_after)
        if not completion["ok"]:
            raise ControlError(f"PROJECT_COMPLETION_NOT_CLOSED: {json.dumps(completion['gaps'], ensure_ascii=False)}")
    return updated, resolution


def commit_report(
    root: Path,
    before: dict[str, dict[str, Any]],
    after: dict[str, dict[str, Any]],
    lease: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    """Classify a committed heartbeat for user-visible reporting.

    SILENT is reserved for a genuinely unchanged/no-op heartbeat.  State
    transitions, external dispatches, and new progress are visible even when
    they do not require user action.
    """

    # A Sol protocol adoption intentionally makes the on-disk specification
    # differ from the pre-commit control snapshot.  Validate the old snapshot's
    # shape without rebinding it to the newly adopted files.
    runtime_before, queue_before, jobs_before = validate_all(root, before, check_contracts=False)
    runtime_after, queue_after, jobs_after = validate_all(root, after)
    job_id = request.get("job_id") or runtime_after.get("active_job_id") or runtime_before.get("active_job_id")
    job_before = jobs_before.get(job_id) if job_id else None
    job_after = jobs_after.get(job_id) if job_id else None
    status_before = job_before.get("status") if job_before else None
    status_after = job_after.get("status") if job_after else None

    material_change = any(
        (
            runtime_before.get("project_status") != runtime_after.get("project_status"),
            runtime_before.get("controller") != runtime_after.get("controller"),
            runtime_before.get("active_job_id") != runtime_after.get("active_job_id"),
            queue_before.get("queue_revision") != queue_after.get("queue_revision"),
            queue_before.get("planning_generation") != queue_after.get("planning_generation"),
            any((queue_before.get("last_empty_event") or {}).get(key)
                != (queue_after.get("last_empty_event") or {}).get(key)
                for key in ("status", "thread_id", "acknowledged_at", "dispatch_receipt", "recovery_requested_at")),
            status_before != status_after,
            (job_before or {}).get("controller", {}).get("semantic_revision")
            != (job_after or {}).get("controller", {}).get("semantic_revision"),
            ((job_before or {}).get("controller", {}).get("current_event") or {}).get("lifecycle")
            != ((job_after or {}).get("controller", {}).get("current_event") or {}).get("lifecycle"),
            ((job_before or {}).get("controller", {}).get("current_event") or {}).get("thread_id")
            != ((job_after or {}).get("controller", {}).get("current_event") or {}).get("thread_id"),
            any(((job_before or {}).get("controller", {}).get("current_event") or {}).get(key)
                != ((job_after or {}).get("controller", {}).get("current_event") or {}).get(key)
                for key in ("dispatch_receipt", "recovery_requested_at", "user_retry_requested_at")),
        )
    )

    finish = request["finish"]
    outcome = str(finish["outcome"])
    upper = outcome.upper()
    previous_outcome = (runtime_before.get("last_supervisor_run") or {}).get("outcome")
    repeated_unchanged = not material_change and previous_outcome == outcome

    policy_violations = (runtime_after.get("last_supervisor_run") or {}).get("policy_violations") or []
    if policy_violations:
        visibility = "ATTENTION"
    elif repeated_unchanged or (upper == "NO_ACTION" and not material_change):
        visibility = "SILENT"
    elif runtime_after.get("project_status") == "COMPLETED" or status_after in {"ACCEPTED", "ARCHIVED"}:
        visibility = "TERMINAL"
    elif any(token in upper for token in ("USER_REQUIRED", "BLOCKED", "FAIL", "LIMIT", "INVALID", "CANCEL", "ERROR", "CAPABILITY_MISMATCH")):
        visibility = "ATTENTION"
    elif material_change or any(
        token in upper for token in ("DISPATCHED", "STARTED", "RUNNING", "CONTINUED", "QUEUED", "REVIEW_PENDING", "RECOVERED")
    ):
        visibility = "PROGRESS"
    else:
        visibility = "SILENT"

    next_action = determine_action(root, runtime_after, queue_after, jobs_after)
    return {
        "visibility": visibility,
        "job_id": job_id,
        "action": lease["planned_action"]["type"],
        "outcome": outcome,
        "status_before": status_before,
        "status_after": status_after,
        "summary": finish.get("summary") or request.get("reason"),
        "next_action": next_action["type"],
        "needs_user": next_action["type"] == "USER_REQUIRED",
        "material_change": material_change,
        "policy_violations": policy_violations,
    }


def commit_request(root: Path, token: str, request_path: Path) -> dict[str, Any]:
    lease = lease_is_current(root, token)
    request = read_json(request_path)
    protocol_file_updates = staged_protocol_file_updates(root, lease, request)
    before = collect_documents(root)
    updated, resolution = apply_request(
        root,
        lease,
        request,
        protocol_file_updates=protocol_file_updates,
    )
    transaction_id = f"{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
    transaction_dir = control_paths(root)["transactions"] / transaction_id
    transaction_dir.mkdir(parents=True, exist_ok=False)
    prepare = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "run_id": lease["run_id"],
        "actor": lease["role"],
        "reason": request.get("reason"),
        "created_at": iso_utc(),
        "before_hashes": lease["snapshot_hashes"],
        "documents": updated,
        "file_updates": protocol_file_updates,
        "intent_resolution": resolution,
    }
    atomic_write_json(transaction_dir / "prepare.json", prepare)
    roll_forward(root, transaction_dir, prepare)
    report = commit_report(root, before, updated, lease, request)
    append_jsonl(
        control_paths(root)["heartbeat_log"],
        {
            "transaction_id": transaction_id,
            "run_id": lease["run_id"],
            "role": lease["role"],
            "started_at": lease["started_at"],
            "finished_at": updated["control/runtime.json"]["last_supervisor_run"]["finished_at"],
            "action": lease["planned_action"]["type"],
            "outcome": updated["control/runtime.json"]["last_supervisor_run"]["outcome"],
            "visibility": report["visibility"],
        },
    )
    lease_path = control_paths(root)["lease"]
    current = read_json(lease_path)
    retain_lease = bool(request.get("retain_lease"))
    retained_expected: dict[str, Any] | None = None
    retained_expires_at: str | None = None
    if current.get("token") == token and retain_lease:
        runtime_after, _queue_after, jobs_after = validate_all(root, updated)
        active_job_id = runtime_after.get("active_job_id")
        retained_expected = {
            "runtime_updated_at": runtime_after["updated_at"],
            "queue_revision": updated["control/queue.json"]["queue_revision"],
            "job_state_revision": jobs_after[active_job_id]["controller"]["state_revision"] if active_job_id else None,
            "job_status": jobs_after[active_job_id]["status"] if active_job_id else None,
        }
        retained_expires_at = iso_utc(
            utc_now() + timedelta(minutes=int(runtime_after["defaults"]["lease_ttl_minutes"]))
        )
        current["snapshot_hashes"] = document_hashes(updated)
        current["expected"] = retained_expected
        current["expires_at"] = retained_expires_at
        current["last_checkpoint_at"] = updated["control/runtime.json"]["last_supervisor_run"]["finished_at"]
        atomic_write_json(lease_path, current)
    elif current.get("token") == token:
        lease_path.unlink()
    result = {
        "ok": True,
        "transaction_id": transaction_id,
        "finished_at": updated["control/runtime.json"]["last_supervisor_run"]["finished_at"],
        "outcome": updated["control/runtime.json"]["last_supervisor_run"]["outcome"],
        "report": report,
    }
    if retain_lease:
        result["lease_retained"] = True
        result["lease_token"] = token
        result["expected"] = retained_expected
        result["expires_at"] = retained_expires_at
    return result


def find_original_acceptance_transaction(root: Path, job_id: str) -> str:
    directory = control_paths(root)["transactions"]
    if directory.exists():
        for path in sorted(item for item in directory.iterdir() if item.is_dir()):
            transaction = _validated_committed_transaction(path)
            if transaction is None:
                continue
            historical = transaction[1].get("documents", {}).get(f"jobs/{job_id}/state.json")
            if isinstance(historical, dict) and historical.get("status") in {"ACCEPTED", "ARCHIVED"}:
                if historical.get("result", {}).get("accepted_at"):
                    return path.name
    raise ControlError(f"legacy accepted job has no committed acceptance snapshot: {job_id}")


def migration_blockers(root: Path, documents: dict[str, dict[str, Any]], *, ignore_lease: bool = False) -> list[dict[str, Any]]:
    blockers: list[dict[str, Any]] = []
    if unresolved_intents(root):
        blockers.append({"code": "MIGRATION_BLOCKED_UNRESOLVED_INTENT"})
    if pending_transactions(root):
        blockers.append({"code": "MIGRATION_BLOCKED_PENDING_TRANSACTION"})
    if not ignore_lease:
        lease = _lease_barrier(root)
        if lease["state"] == "LIVE":
            blockers.append({"code": "MIGRATION_BLOCKED_LIVE_LEASE"})
        elif lease["state"] == "EXPIRED":
            blockers.append({"code": "STALE_LEASE_RECOVERY_REQUIRED"})
    for rel_path, job in documents.items():
        if not (rel_path.startswith("jobs/") and rel_path.endswith("/state.json")):
            continue
        escalation = job.get("controller", {}).get("escalation") or {}
        if escalation.get("required") or escalation.get("status") in {"REQUIRED", "DISPATCHED", "ACKNOWLEDGED"}:
            blockers.append(
                {
                    "code": "MIGRATION_BLOCKED_OPEN_EVENT",
                    "job_id": job.get("job_id"),
                    "event_id": escalation.get("event_id") or job.get("controller", {}).get("event_id"),
                }
            )
    return blockers


def build_v81_migration_documents(
    root: Path,
    documents: dict[str, dict[str, Any]],
    migrated_at: str,
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    runtime = documents["control/runtime.json"]
    queue = documents["control/queue.json"]
    if runtime.get("schema_version") != 6 or runtime.get("controller", {}).get("workflow_version") != 8:
        raise ControlError("schema is not a workflow v8 store")
    if queue.get("schema_version") != 3:
        raise ControlError("queue is not workflow v8 schema")
    migrated = copy.deepcopy(documents)
    migrated_runtime = migrated["control/runtime.json"]
    migrated_runtime["schema_version"] = RUNTIME_SCHEMA
    migrated_runtime["controller"]["workflow_version"] = WORKFLOW_VERSION
    migrated_runtime["controller"].pop("completion_gaps", None)
    migrated_runtime.setdefault("state_store", {}).update(
        {"migration": "V8_TO_V8_1_COMPLETE", "migrated_at": migrated_at, "version": 2}
    )
    migrated_runtime["updated_at"] = migrated_at
    migrated_runtime["last_supervisor_run"] = {
        "started_at": migrated_at,
        "finished_at": migrated_at,
        "observed_duration_seconds": 0.0,
        "source_thread_id": None,
        "role": "MIGRATION",
        "outcome": "WORKFLOW_V8_1_READY",
        "job_id": migrated_runtime.get("active_job_id"),
        "action": "MIGRATE_V81",
    }
    migrated_queue = migrated["control/queue.json"]
    migrated_queue["schema_version"] = QUEUE_SCHEMA
    migrated_queue["queue_revision"] = int(migrated_queue["queue_revision"]) + 1
    migrated_queue["updated_at"] = migrated_at

    report: dict[str, Any] = {"migrated_jobs": [], "legacy_acceptances": [], "preserved_event_ids": []}
    for rel_path, old_job in sorted(documents.items()):
        if not (rel_path.startswith("jobs/") and rel_path.endswith("/state.json")):
            continue
        if old_job.get("schema_version") != 4:
            raise ControlError(f"job is not workflow v8 schema: {old_job.get('job_id')}")
        job = copy.deepcopy(old_job)
        job["schema_version"] = JOB_SCHEMA
        controller = job["controller"]
        baseline = int(old_job["controller"]["state_revision"])
        legacy_event_id = controller.pop("event_id", None)
        legacy_escalation = controller.pop("escalation", {}) or {}
        controller["state_revision"] = baseline + 1
        controller["semantic_revision"] = baseline
        controller["event_generation"] = baseline
        controller["current_event"] = None
        if legacy_event_id:
            controller.setdefault("legacy_event_ids", []).append(legacy_event_id)
            report["preserved_event_ids"].append({"job_id": job["job_id"], "event_id": legacy_event_id})
        escalation_event_id = legacy_escalation.get("event_id")
        if escalation_event_id and legacy_escalation.get("status") in {"RESOLVED", "USER_REQUIRED"}:
            match = re.fullmatch(r"[^:]+:(\d+):(BLOCKED|FAILED)", escalation_event_id)
            generation = int(match.group(1)) if match else baseline
            origin_status = match.group(2) if match else "BLOCKED"
            root_cause = (
                legacy_escalation.get("reason")
                or controller.get("escalation_metrics", {}).get("current_root_cause_signature")
                or "LEGACY_UNCLASSIFIED"
            )
            identity_job = copy.deepcopy(job)
            identity_job["status"] = origin_status
            controller["current_event"] = {
                "event_id": escalation_event_id,
                "generation": generation,
                "origin_status": origin_status,
                "origin_semantic_revision": min(generation, baseline),
                "event_key": technical_event_key(identity_job, str(root_cause)),
                "root_cause_signature": str(root_cause),
                "resource_identities": [],
                "lifecycle": "RESOLVED",
                "dispatched_at": legacy_escalation.get("dispatched_at"),
                "acknowledged_at": legacy_escalation.get("acknowledged_at"),
                "resolved_at": migrated_at,
                "thread_id": legacy_escalation.get("thread_id"),
                "migration_source": "LEGACY_V8_ESCALATION",
            }
            controller["event_generation"] = max(baseline, generation)
        job["user_gate"] = None
        if job.get("decision", {}).get("needs_user"):
            job["user_gate"] = make_user_gate(job)
        if job["status"] in {"ACCEPTED", "ARCHIVED"}:
            original_verification = copy.deepcopy(job.get("verification") or {})
            pre_migration_verification_hash = digest_json(original_verification)
            original_transaction_id = find_original_acceptance_transaction(root, job["job_id"])
            original_transaction = _validated_committed_transaction(
                control_paths(root)["transactions"] / original_transaction_id
            )
            accepted_verification = original_transaction[1]["documents"][rel_path].get("verification") or {}
            job["verification"] = original_verification
            job["verification"]["record_version"] = "LEGACY_V8"
            job["verification"]["legacy_migration"] = {
                "original_accept_transaction_id": original_transaction_id,
                "original_verification_sha256": digest_json(accepted_verification),
                "pre_migration_verification_sha256": pre_migration_verification_hash,
                "migrated_at": migrated_at,
            }
            report["legacy_acceptances"].append(
                {
                    "job_id": job["job_id"],
                    "transaction_id": original_transaction_id,
                    "original_verification_sha256": digest_json(accepted_verification),
                    "pre_migration_verification_sha256": pre_migration_verification_hash,
                }
            )
        job["updated_at"] = migrated_at
        migrated[rel_path] = job
        report["migrated_jobs"].append(
            {
                "job_id": job["job_id"],
                "old_state_revision": baseline,
                "new_state_revision": baseline + 1,
                "semantic_revision_baseline": baseline,
                "event_generation_baseline": baseline,
                "dsh_lifecycle_class": dsh_lifecycle_class(job["dsh"]),
            }
        )
    return migrated, report


def migrate_v81(root: Path, *, dry_run: bool = False) -> dict[str, Any]:
    documents = collect_documents(root)
    blockers = migration_blockers(root, documents)
    migrated_at = iso_utc()
    if blockers:
        return {"ok": False, "status": "MIGRATION_BLOCKED", "blockers": blockers, "dry_run": dry_run}
    migrated, report = build_v81_migration_documents(root, documents, migrated_at)
    validate_all(root, migrated)
    before_hashes = document_hashes(documents)
    after_hashes = document_hashes(migrated)
    if dry_run:
        return {
            "ok": True,
            "status": "DRY_RUN_READY",
            "dry_run": True,
            "before_hashes": before_hashes,
            "after_hashes": after_hashes,
            "report": report,
        }

    paths = control_paths(root)
    started = utc_now()
    lease = {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "token": str(uuid.uuid4()),
        "role": "MIGRATION",
        "source_thread_id": None,
        "bound_event_id": None,
        "started_at": iso_utc(started),
        "expires_at": iso_utc(started + timedelta(minutes=60)),
        "migration": "V8_TO_V8_1",
    }
    try:
        exclusive_write_json(paths["lease"], lease)
    except ControlError:
        return {"ok": False, "status": "MIGRATION_BLOCKED", "blockers": [{"code": "MIGRATION_BLOCKED_LIVE_LEASE"}]}
    try:
        fresh = collect_documents(root)
        if document_hashes(fresh) != before_hashes:
            raise ControlError("migration source changed after dry-run validation")
        blockers = migration_blockers(root, fresh, ignore_lease=True)
        if blockers:
            raise ControlError(blockers[0]["code"])
        transaction_id = f"{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"
        transaction_dir = paths["transactions"] / transaction_id
        transaction_dir.mkdir(parents=True, exist_ok=False)
        prepare = {
            "schema_version": 1,
            "transaction_id": transaction_id,
            "run_id": lease["run_id"],
            "actor": "MIGRATION",
            "reason": "USER_AUTHORIZED_WORKFLOW_V8_TO_V8_1",
            "created_at": migrated_at,
            "before_hashes": before_hashes,
            "documents": migrated,
            "intent_resolution": None,
            "migration_report": report,
        }
        atomic_write_json(transaction_dir / "prepare.json", prepare)
        roll_forward(root, transaction_dir, prepare)
        append_jsonl(
            paths["heartbeat_log"],
            {
                "transaction_id": transaction_id,
                "run_id": lease["run_id"],
                "role": "MIGRATION",
                "started_at": lease["started_at"],
                "finished_at": iso_utc(),
                "action": "MIGRATE_V81",
                "outcome": "WORKFLOW_V8_1_READY",
                "visibility": "PROGRESS",
            },
        )
        return {"ok": True, "status": "MIGRATED", "transaction_id": transaction_id, "report": report}
    finally:
        if paths["lease"].exists():
            try:
                if read_json(paths["lease"]).get("token") == lease["token"]:
                    paths["lease"].unlink()
            except ControlError:
                pass


def bootstrap(root: Path) -> dict[str, Any]:
    if latest_transaction(root) is not None:
        raise ControlError("state store is already bootstrapped")
    documents = collect_documents(root)
    validate_all(root, documents)
    transaction_id = f"{utc_now().strftime('%Y%m%dT%H%M%S%fZ')}-bootstrap"
    transaction_dir = control_paths(root)["transactions"] / transaction_id
    transaction_dir.mkdir(parents=True, exist_ok=False)
    prepare = {
        "schema_version": 1,
        "transaction_id": transaction_id,
        "run_id": "migration-v8",
        "actor": "MIGRATION",
        "reason": "BOOTSTRAP_V8_STATE_STORE",
        "created_at": iso_utc(),
        "before_hashes": document_hashes(documents),
        "documents": documents,
        "intent_resolution": None,
    }
    atomic_write_json(transaction_dir / "prepare.json", prepare)
    roll_forward(root, transaction_dir, prepare)
    append_jsonl(
        control_paths(root)["heartbeat_log"],
        {"transaction_id": transaction_id, "role": "MIGRATION", "action": "BOOTSTRAP", "outcome": "V8_READY", "finished_at": iso_utc()},
    )
    return {"ok": True, "transaction_id": transaction_id, "document_hashes": document_hashes(documents)}


def _diagnostic_status(exc: ControlError) -> str:
    message = str(exc).lower()
    if "outside supervisor_ctl" in message:
        return "STATE_DRIFT"
    if any(token in message for token in ("contract", "authority", "specification hash")):
        return "CONTRACT_MISMATCH"
    return "SCHEMA_ERROR"


def check(root: Path) -> dict[str, Any]:
    """Read-only, double-barrier control-plane diagnosis."""
    barrier_1 = read_transaction_barrier(root)
    if barrier_1["lease"]["state"] == "LIVE":
        return {"ok": False, "status": "BUSY", "barrier": barrier_1}
    if barrier_1["lease"]["state"] == "EXPIRED":
        return {"ok": False, "status": "STALE_LEASE_RECOVERY_REQUIRED", "barrier": barrier_1}

    candidate: dict[str, Any]
    try:
        documents = collect_documents(root)
        verify_transaction_snapshot(root, documents)
        runtime, queue, jobs = validate_all(root, documents)
        action = determine_action(root, runtime, queue, jobs)
        candidate = {
            "ok": True,
            "status": "OK",
            "workflow_version": runtime["controller"]["workflow_version"],
            "project_status": runtime["project_status"],
            "active_job_id": runtime["active_job_id"],
            "queue_revision": queue["queue_revision"],
            "job_count": len(jobs),
            "unresolved_intents": len(unresolved_intents(root)),
            "next_action_preview": {**action, "advisory_only": True},
            "next_action": action,
        }
    except ControlError as exc:
        candidate = {"ok": False, "status": _diagnostic_status(exc), "error": str(exc)}

    barrier_2 = read_transaction_barrier(root)
    if barrier_2["lease"]["state"] == "LIVE":
        return {"ok": False, "status": "BUSY", "barrier": barrier_2}
    if barrier_2["lease"]["state"] == "EXPIRED":
        return {"ok": False, "status": "STALE_LEASE_RECOVERY_REQUIRED", "barrier": barrier_2}
    if barrier_1 != barrier_2:
        return {"ok": False, "status": "RETRY_CONCURRENT_CHANGE", "barrier_1": barrier_1, "barrier_2": barrier_2}
    pending = pending_transactions(root)
    if pending:
        return {
            "ok": False,
            "status": "RECOVERY_REQUIRED",
            "pending_transactions": [item["transaction_id"] for item in pending],
            "barrier": barrier_2,
        }
    candidate["barrier"] = barrier_2
    return candidate


def verify(root: Path) -> dict[str, Any]:
    """Compatibility alias for the read-only check command."""
    return check(root)


def reclaim_abandoned_luna(root: Path, run_id: str, authorization: str) -> None:
    """Explicit user maintenance only; fence a named abandoned routing lease."""
    if not authorization.strip():
        raise ControlError("USER_AUTHORIZATION_REQUIRED")
    paths = control_paths(root)
    lease = read_json(paths["lease"])
    if lease.get("run_id") != run_id:
        raise ControlError("ABANDONED_RUN_MISMATCH")
    if lease.get("role") != "LUNA" or lease.get("source_thread_id"):
        raise ControlError("RECLAIM_REQUIRES_UNBOUND_LUNA")
    if lease.get("intent_id"):
        raise ControlError("RECLAIM_HAS_EXTERNAL_INTENT")
    deadline = lease.get("routing_action_deadline_at")
    if not deadline or parse_time(deadline) >= utc_now():
        raise ControlError("RECLAIM_ROUTING_DEADLINE_NOT_EXCEEDED")
    receipt_dir = root / "control" / "diagnostics" / "user-lease-recovery"
    receipt_dir.mkdir(parents=True, exist_ok=True)
    receipt = receipt_dir / f"{run_id}-{uuid.uuid4().hex[:8]}.json"
    exclusive_write_json(receipt, {"authorization_basis": authorization,
        "recorded_at": iso_utc(), "lease": lease,
        "reason": "User-confirmed ended routing task; no commit; revoke old token before recovery."})
    if read_json(paths["lease"]) != lease:
        raise ControlError("RECLAIM_LEASE_CHANGED")
    paths["stale_leases"].mkdir(parents=True, exist_ok=True)
    os.replace(paths["lease"], paths["stale_leases"] / f"{run_id}-user-{uuid.uuid4().hex[:8]}.json")


def recover(root: Path) -> dict[str, Any]:
    """Acquire a dedicated recovery lease and roll forward prepared snapshots."""
    paths = control_paths(root)
    lease_path = paths["lease"]
    if lease_path.exists():
        lease = read_json(lease_path)
        if parse_time(lease["expires_at"]) > utc_now():
            return {"ok": False, "status": "BUSY"}
        paths["stale_leases"].mkdir(parents=True, exist_ok=True)
        os.replace(lease_path, paths["stale_leases"] / f"{lease.get('run_id', 'unknown')}-{uuid.uuid4().hex[:8]}.json")
    started = utc_now()
    lease = {
        "schema_version": 1,
        "run_id": str(uuid.uuid4()),
        "token": str(uuid.uuid4()),
        "role": "MIGRATION",
        "source_thread_id": None,
        "bound_event_id": None,
        "started_at": iso_utc(started),
        "expires_at": iso_utc(started + timedelta(minutes=60)),
        "recovery_only": True,
    }
    try:
        exclusive_write_json(lease_path, lease)
    except ControlError:
        return {"ok": False, "status": "BUSY"}
    try:
        recovered = recover_transactions(root)
        documents = collect_documents(root)
        verify_transaction_snapshot(root, documents)
        validate_all(root, documents)
        return {"ok": True, "status": "RECOVERED", "recovered_transactions": recovered}
    finally:
        if lease_path.exists():
            try:
                if read_json(lease_path).get("token") == lease["token"]:
                    lease_path.unlink()
            except ControlError:
                pass


def output(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    subparsers = parser.add_subparsers(dest="command", required=True)

    begin = subparsers.add_parser("begin", help="acquire the single-writer lease and return one action")
    begin.add_argument("--role", choices=sorted(ROLE_VALUES), required=True)
    begin.add_argument("--source-thread-id")
    begin.add_argument("--event-id")
    begin.add_argument(
        "--wait-lease-seconds",
        type=float,
        default=0,
        help="for Sol only, wait briefly for the dispatching Luna lease to be released",
    )

    renew = subparsers.add_parser("renew", help="renew the current lease")
    renew.add_argument("--token", required=True)

    prepare = subparsers.add_parser("prepare-intent", help="persist the planned external action before calling it")
    prepare.add_argument("--token", required=True)

    preflight = subparsers.add_parser("capability-preflight", help="verify actual handler Full Access and workspace capabilities")
    preflight.add_argument("--token", required=True)

    commit = subparsers.add_parser("commit", help="commit a validated patch transaction and finish the run")
    commit.add_argument("--token", required=True)
    commit.add_argument("--request", type=Path, required=True)

    subparsers.add_parser("bootstrap", help="create the initial committed v8 snapshot")
    subparsers.add_parser("check", help="read-only double-barrier state and contract diagnosis")
    subparsers.add_parser("verify", help="compatibility alias for check")
    recovery = subparsers.add_parser("recover", help="acquire a recovery lease and roll forward pending transactions")
    recovery.add_argument("--abandoned-run-id", help="user maintenance only: revoke this exact abandoned unbound Luna routing lease")
    recovery.add_argument("--user-authorization", default="")
    migrate = subparsers.add_parser("migrate-v81", help="migrate a workflow v8 store through one USER/MIGRATION transaction")
    migrate.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = args.root.resolve()
    try:
        if args.command == "begin":
            result = acquire_lease(
                root,
                args.role,
                args.source_thread_id,
                args.event_id,
                wait_lease_seconds=args.wait_lease_seconds,
            )
        elif args.command == "renew":
            result = renew_lease(root, args.token)
        elif args.command == "prepare-intent":
            result = prepare_intent(root, args.token)
        elif args.command == "capability-preflight":
            from control.handler_capabilities import preflight
            result = preflight(root, lease_is_current(root, args.token))
        elif args.command == "commit":
            result = commit_request(root, args.token, args.request.resolve())
        elif args.command == "bootstrap":
            result = bootstrap(root)
        elif args.command in {"check", "verify"}:
            result = check(root)
        elif args.command == "recover":
            if args.abandoned_run_id:
                reclaim_abandoned_luna(root, args.abandoned_run_id, args.user_authorization)
            result = recover(root)
        elif args.command == "migrate-v81":
            result = migrate_v81(root, dry_run=args.dry_run)
        else:  # pragma: no cover
            parser.error("unknown command")
            return 2
        output(result)
        return 0
    except ControlError as exc:
        output({"ok": False, "error": str(exc)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
