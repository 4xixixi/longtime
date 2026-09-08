from __future__ import annotations

import copy
import json
import os
import time
import unittest
from pathlib import Path
from unittest import mock

from control import supervisor_ctl as ctl
from tests.test_supervisor_ctl import NOW, make_root, write_json


def rewrite_committed_head(root: Path, documents: dict[str, dict]) -> None:
    for rel_path, document in documents.items():
        write_json(root / rel_path, document)
    transaction_dir, prepare, _ = ctl.latest_transaction(root)
    prepare["documents"] = copy.deepcopy(documents)
    write_json(transaction_dir / "prepare.json", prepare)
    commit = ctl.read_json(transaction_dir / "commit.json")
    commit["document_hashes"] = ctl.document_hashes(documents)
    write_json(transaction_dir / "commit.json", commit)


def passing_verification(root: Path, review_id: str = "review-v81") -> dict:
    protocol_path = root / "jobs" / "job-one" / "protocol-contract.json"
    protocol = ctl.read_json(protocol_path)
    return {
        "record_version": "V8_1",
        "provenance": "LUNA_ATTESTED",
        "review_run_id": review_id,
        "specification_sha256": protocol["specification_sha256"],
        "protocol_contract_sha256": ctl.digest_file(protocol_path),
        "started_at": NOW,
        "completed_at": NOW,
        "result": "PASSED",
        "commands": [
            {
                "command_id": "verify-001",
                "command": "true",
                "exit_code": 0,
                "key_output": "ok",
                "evidence_paths": [],
                "evidence_sha256": [],
            }
        ],
        "unverified_items": [],
    }


def commit_job(root: Path, patch: dict, *, event_update: dict | None = None) -> dict:
    run = ctl.acquire_lease(root, "LUNA", "thread-luna", None)
    request = {
        "reason": "test",
        "job_id": "job-one",
        "expected": run["expected"],
        "patches": {"job": patch},
        "finish": {"outcome": "TEST"},
    }
    if event_update is not None:
        request["event_update"] = event_update
    path = root / "request.json"
    write_json(path, request)
    return ctl.commit_request(root, run["lease_token"], path)


def make_legacy_root(*, status: str = "QUEUED", active: bool = True, queued: bool = True) -> Path:
    root = make_root(active=active, queued=queued, job_status=status)
    documents = ctl.collect_documents(root)
    runtime = documents["control/runtime.json"]
    runtime["schema_version"] = 6
    runtime["controller"]["workflow_version"] = 8
    runtime.setdefault("state_store", {"version": 1, "migration": "V7_TO_V8_COMPLETE"})
    queue = documents["control/queue.json"]
    queue["schema_version"] = 3
    for rel_path, job in documents.items():
        if not rel_path.startswith("jobs/"):
            continue
        job["schema_version"] = 4
        controller = job["controller"]
        controller["event_id"] = f"{job['job_id']}:{controller['state_revision']}:{job['status']}"
        event = controller.pop("current_event", None)
        controller.pop("semantic_revision", None)
        controller.pop("event_generation", None)
        controller.pop("event_history", None)
        controller.pop("legacy_event_ids", None)
        controller["escalation"] = {}
        if event:
            controller["escalation"] = {
                "required": event["lifecycle"] in ctl.OPEN_EVENT_LIFECYCLES,
                "status": event["lifecycle"],
                "event_id": event["event_id"],
                "thread_id": event.get("thread_id"),
                "reason": event.get("root_cause_signature"),
            }
        job.pop("user_gate", None)
        if job["status"] in {"ACCEPTED", "ARCHIVED"}:
            job["verification"] = {
                "commands": ["legacy verify"],
                "results": [{"exit_code": 0}],
                "unverified_items": [],
            }
    rewrite_committed_head(root, documents)
    return root


def make_handoff_root(*, last: bool = False) -> Path:
    root = make_root(job_status="ACCEPTED")
    if last:
        return root
    documents = ctl.collect_documents(root)
    project = documents["control/project-contract.json"]
    project["required_queue_order"] = ["job-one", "job-two"]
    write_json(root / "control" / "project-contract.json", project)
    project_hash = ctl.digest_file(root / "control" / "project-contract.json")
    documents["control/runtime.json"]["controller"]["project_contract_sha256"] = project_hash

    first_protocol_path = root / "jobs" / "job-one" / "protocol-contract.json"
    first_protocol = ctl.read_json(first_protocol_path)
    first_protocol["project_contract_sha256"] = project_hash
    write_json(first_protocol_path, first_protocol)
    first_contract_hash = ctl.digest_file(first_protocol_path)
    first = documents["jobs/job-one/state.json"]
    first["controller"]["protocol_contract_sha256"] = first_contract_hash
    first["verification"]["protocol_contract_sha256"] = first_contract_hash
    documents["control/queue.json"]["jobs"][0].update(
        {
            "specification_sha256": first_protocol["specification_sha256"],
            "protocol_contract_sha256": first_contract_hash,
        }
    )

    spec_path = root / "jobs" / "job-two" / "specification.md"
    spec_path.parent.mkdir(parents=True)
    spec_path.write_text("# successor\n", encoding="utf-8")
    protocol = {
        "schema_version": 1,
        "job_id": "job-two",
        "project_contract_sha256": project_hash,
        "specification_sha256": ctl.digest_file(spec_path),
        "protocol_revision": 1,
        "protocol_authority_sha256": "b" * 64,
        "canonical_replay_a": None,
        "canonical_replay_b": None,
        "canonical_formal": "formal-two",
        "predecessors": ["job-one"],
    }
    protocol_path = root / "jobs" / "job-two" / "protocol-contract.json"
    write_json(protocol_path, protocol)
    successor = copy.deepcopy(first)
    successor.update({"job_id": "job-two", "title": "two", "status": "QUEUED"})
    successor["authorized_specification"].update(
        {
            "current_sha256": protocol["specification_sha256"],
            "protocol_revision": 1,
            "protocol_authority_sha256": "b" * 64,
            "canonical_replay_a": None,
            "canonical_replay_b": None,
            "canonical_formal": "formal-two",
        }
    )
    successor["dsh"].update(
        {
            "session_id": None,
            "lifecycle_status": None,
            "continuation_required": False,
            "continuation_instruction": None,
            "last_result_summary": None,
        }
    )
    successor["controller"].update(
        {
            "state_revision": 1,
            "semantic_revision": 1,
            "event_generation": 0,
            "current_event": None,
            "last_handled_event_id": None,
            "protocol_contract_sha256": ctl.digest_file(protocol_path),
        }
    )
    successor["verification"] = {"commands": [], "results": [], "unverified_items": []}
    successor["decision"] = {"needs_user": False, "user_only_gate": None, "route": None, "reason": None}
    successor["result"] = {"accepted_at": None, "summary": None, "changed_files": []}
    successor["user_gate"] = None
    documents["jobs/job-two/state.json"] = successor
    documents["control/queue.json"]["jobs"].append(
        {
            "job_id": "job-two",
            "priority": 2,
            "queued_at": NOW,
            "specification_sha256": protocol["specification_sha256"],
            "protocol_contract_sha256": ctl.digest_file(protocol_path),
        }
    )
    rewrite_committed_head(root, documents)
    return root


class RevisionAndAcceptanceTests(unittest.TestCase):
    def test_observation_patch_only_increments_state_revision(self) -> None:
        root = make_root()
        before = ctl.read_json(root / "jobs" / "job-one" / "state.json")["controller"]
        commit_job(root, {"dsh": {"last_checked_at": "2026-09-02T00:01:00.000Z"}})
        after = ctl.read_json(root / "jobs" / "job-one" / "state.json")["controller"]
        self.assertEqual(after["state_revision"], before["state_revision"] + 1)
        self.assertEqual(after["semantic_revision"], before["semantic_revision"])

    def test_semantic_change_increments_semantic_revision(self) -> None:
        root = make_root()
        commit_job(root, {"status": "RUNNING", "dsh": {"lifecycle_status": "running"}})
        controller = ctl.read_json(root / "jobs" / "job-one" / "state.json")["controller"]
        self.assertEqual(controller["semantic_revision"], 2)

    def test_dispatch_bookkeeping_preserves_event_id(self) -> None:
        root = make_root(job_status="BLOCKED")
        before = ctl.read_json(root / "jobs" / "job-one" / "state.json")["controller"]
        commit_job(root, {}, event_update={"lifecycle": "DISPATCHED", "thread_id": "sol"})
        after = ctl.read_json(root / "jobs" / "job-one" / "state.json")["controller"]
        self.assertEqual(after["current_event"]["event_id"], before["current_event"]["event_id"])
        self.assertEqual(after["event_generation"], before["event_generation"])
        self.assertEqual(after["semantic_revision"], before["semantic_revision"])

    def test_same_open_event_does_not_increment_generation(self) -> None:
        root = make_root(job_status="BLOCKED")
        before = ctl.read_json(root / "jobs" / "job-one" / "state.json")["controller"]["event_generation"]
        commit_job(root, {"decision": {"reason": "more detail"}})
        after = ctl.read_json(root / "jobs" / "job-one" / "state.json")["controller"]["event_generation"]
        self.assertEqual(after, before)

    def test_reoccurring_closed_event_increments_generation(self) -> None:
        root = make_root(job_status="BLOCKED")
        luna = ctl.acquire_lease(root, "LUNA", "luna", None)
        event_id = luna["planned_action"]["event_id"]
        path = root / "dispatch.json"
        write_json(path, {"reason": "dispatch", "patches": {"job": {}}, "event_update": {"lifecycle": "DISPATCHED", "thread_id": "sol"}, "finish": {"outcome": "DISPATCHED"}})
        ctl.commit_request(root, luna["lease_token"], path)
        sol = ctl.acquire_lease(root, "SOL", "sol", event_id)
        write_json(path, {"reason": "fixed", "patches": {"job": {"status": "REVIEW_PENDING", "decision": {"route": "REVIEW"}}}, "finish": {"outcome": "RESOLVED"}})
        ctl.commit_request(root, sol["lease_token"], path)
        commit_job(root, {"status": "BLOCKED", "decision": {"route": "SOL_ESCALATE", "error_code": "UNCLASSIFIED"}})
        controller = ctl.read_json(root / "jobs" / "job-one" / "state.json")["controller"]
        self.assertEqual(controller["event_generation"], 2)
        self.assertEqual(controller["semantic_revision"], 3)

    def test_event_identity_fields_are_controller_owned(self) -> None:
        root = make_root()
        run = ctl.acquire_lease(root, "LUNA", "luna", None)
        path = root / "bad.json"
        write_json(path, {"reason": "bad", "patches": {"job": {"controller": {"event_generation": 9}}}, "finish": {"outcome": "BAD"}})
        with self.assertRaisesRegex(ctl.ControlError, "CONTROLLER_OWNED"):
            ctl.commit_request(root, run["lease_token"], path)

    def test_sol_cannot_transition_to_accepted(self) -> None:
        root = make_root(job_status="REVIEW_PENDING")
        run = ctl.acquire_lease(root, "LUNA", "luna", None)
        lease_path = root / "control" / "run-lease.json"
        lease = ctl.read_json(lease_path)
        lease["role"] = "SOL"
        write_json(lease_path, lease)
        path = root / "bad.json"
        write_json(path, {"reason": "bad", "patches": {"job": {"status": "ACCEPTED", "verification": passing_verification(root), "result": {"accepted_at": NOW}}}, "finish": {"outcome": "BAD"}})
        with self.assertRaisesRegex(ctl.ControlError, "ROLE_NOT_AUTHORIZED_FOR_ACCEPTANCE"):
            ctl.commit_request(root, run["lease_token"], path)

    def test_luna_accept_requires_review_action(self) -> None:
        root = make_root(job_status="REVIEW_PENDING")
        run = ctl.acquire_lease(root, "LUNA", "luna", None)
        lease_path = root / "control" / "run-lease.json"
        lease = ctl.read_json(lease_path)
        lease["planned_action"] = {"type": "DSH_STATUS"}
        write_json(lease_path, lease)
        path = root / "bad.json"
        write_json(path, {"reason": "bad", "patches": {"job": {"status": "ACCEPTED", "verification": passing_verification(root), "result": {"accepted_at": NOW}}}, "finish": {"outcome": "BAD"}})
        with self.assertRaisesRegex(ctl.ControlError, "ACCEPTANCE_REQUIRES_REVIEW_ACTION"):
            ctl.commit_request(root, run["lease_token"], path)

    def test_luna_accept_requires_complete_passing_verification_record(self) -> None:
        root = make_root(job_status="REVIEW_PENDING")
        with self.assertRaisesRegex(ctl.ControlError, "ACCEPTANCE_RECORD_INCOMPLETE"):
            commit_job(root, {"status": "ACCEPTED", "verification": {"record_version": "V8_1"}, "result": {"accepted_at": NOW}})

    def test_roll_forward_replays_prepared_accepted_snapshot(self) -> None:
        root = make_root(job_status="REVIEW_PENDING")
        run = ctl.acquire_lease(root, "LUNA", "luna", None)
        request = {
            "reason": "accept",
            "patches": {"job": {"status": "ACCEPTED", "verification": passing_verification(root), "result": {"accepted_at": NOW}}},
            "finish": {"outcome": "ACCEPTED"},
        }
        updated, resolution = ctl.apply_request(root, ctl.read_json(root / "control" / "run-lease.json"), request)
        transaction = root / "control" / "transactions" / "99999999T999999999999Z-accept"
        transaction.mkdir()
        write_json(transaction / "prepare.json", {"transaction_id": transaction.name, "documents": updated, "intent_resolution": resolution})
        (root / "control" / "run-lease.json").unlink()
        self.assertTrue(ctl.recover(root)["ok"])
        self.assertEqual(ctl.read_json(root / "jobs" / "job-one" / "state.json")["status"], "ACCEPTED")

    def test_legacy_accepted_record_is_not_rewritten_as_v81_attestation(self) -> None:
        root = make_legacy_root(status="ARCHIVED", active=False, queued=False)
        self.assertTrue(ctl.migrate_v81(root)["ok"])
        verification = ctl.read_json(root / "jobs" / "job-one" / "state.json")["verification"]
        self.assertEqual(verification["record_version"], "LEGACY_V8")
        self.assertNotEqual(verification.get("provenance"), "LUNA_ATTESTED")


class CheckRecoverTests(unittest.TestCase):
    def test_check_is_application_level_read_only(self) -> None:
        root = make_root()
        before = {str(path.relative_to(root)): ctl.digest_file(path) for path in root.rglob("*") if path.is_file()}
        self.assertEqual(ctl.check(root)["status"], "OK")
        after = {str(path.relative_to(root)): ctl.digest_file(path) for path in root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)

    def test_check_returns_busy_if_lease_appears_between_barriers(self) -> None:
        root = make_root()
        original = ctl.read_transaction_barrier(root)
        live = copy.deepcopy(original)
        live["lease"] = {"state": "LIVE", "sha256": "x"}
        with mock.patch.object(ctl, "read_transaction_barrier", side_effect=[original, live]):
            self.assertEqual(ctl.check(root)["status"], "BUSY")

    def test_check_returns_retry_if_commit_head_changes(self) -> None:
        root = make_root()
        first = ctl.read_transaction_barrier(root)
        second = copy.deepcopy(first)
        second["committed_head"]["commit_sha256"] = "changed"
        with mock.patch.object(ctl, "read_transaction_barrier", side_effect=[first, second]):
            self.assertEqual(ctl.check(root)["status"], "RETRY_CONCURRENT_CHANGE")

    def test_check_returns_stale_lease_recovery_required(self) -> None:
        root = make_root()
        write_json(root / "control" / "run-lease.json", {"expires_at": "2000-01-01T00:00:00.000Z"})
        self.assertEqual(ctl.check(root)["status"], "STALE_LEASE_RECOVERY_REQUIRED")

    def test_state_drift_requires_stable_double_barrier(self) -> None:
        root = make_root()
        runtime = ctl.read_json(root / "control" / "runtime.json")
        runtime["project_status"] = "FAILED"
        write_json(root / "control" / "runtime.json", runtime)
        self.assertEqual(ctl.check(root)["status"], "STATE_DRIFT")

    def test_pending_transaction_is_not_committed_head(self) -> None:
        root = make_root()
        head = ctl.latest_committed_transaction(root)[0].name
        pending = root / "control" / "transactions" / "99999999T999999999999Z-pending"
        pending.mkdir()
        write_json(pending / "prepare.json", {"transaction_id": pending.name, "documents": ctl.collect_documents(root)})
        self.assertEqual(ctl.latest_committed_transaction(root)[0].name, head)
        self.assertEqual(ctl.check(root)["status"], "RECOVERY_REQUIRED")

    def test_recover_rolls_forward_pending_transaction(self) -> None:
        root = make_root()
        documents = ctl.collect_documents(root)
        documents["control/runtime.json"]["next_expected_run_at"] = "2030-01-01T00:00:00.000Z"
        pending = root / "control" / "transactions" / "99999999T999999999999Z-pending"
        pending.mkdir()
        write_json(pending / "prepare.json", {"transaction_id": pending.name, "documents": documents, "intent_resolution": None})
        self.assertTrue(ctl.recover(root)["ok"])
        self.assertEqual(ctl.read_json(root / "control" / "runtime.json")["next_expected_run_at"], "2030-01-01T00:00:00.000Z")


class HandoffCompletionTests(unittest.TestCase):
    def _commit_handoff(self, root: Path) -> dict:
        run = ctl.acquire_lease(root, "LUNA", "luna", None)
        self.assertEqual(run["planned_action"]["type"], "HANDOFF_ACCEPTED_JOB")
        path = root / "handoff.json"
        write_json(path, {"reason": "handoff", "patches": {}, "finish": {"outcome": "HANDOFF_COMPLETE"}})
        return ctl.commit_request(root, run["lease_token"], path)

    def test_handoff_archives_and_activates_successor_atomically(self) -> None:
        root = make_handoff_root()
        self._commit_handoff(root)
        runtime = ctl.read_json(root / "control" / "runtime.json")
        queue = ctl.read_json(root / "control" / "queue.json")
        self.assertEqual(runtime["active_job_id"], "job-two")
        self.assertEqual([entry["job_id"] for entry in queue["jobs"]], ["job-two"])
        self.assertEqual(ctl.read_json(root / "jobs" / "job-one" / "state.json")["status"], "ARCHIVED")

    def test_handoff_rejects_unready_successor_without_partial_commit(self) -> None:
        root = make_handoff_root()
        before = ctl.document_hashes(ctl.collect_documents(root))
        documents = ctl.collect_documents(root)
        documents["jobs/job-two/state.json"]["status"] = "RUNNING"
        rewrite_committed_head(root, documents)
        before = ctl.document_hashes(ctl.collect_documents(root))
        run = ctl.acquire_lease(root, "LUNA", "luna", None)
        path = root / "handoff.json"
        write_json(path, {"reason": "handoff", "patches": {}, "finish": {"outcome": "HANDOFF"}})
        with self.assertRaisesRegex(ctl.ControlError, "HANDOFF_SUCCESSOR_NOT_QUEUED"):
            ctl.commit_request(root, run["lease_token"], path)
        self.assertEqual(ctl.document_hashes(ctl.collect_documents(root)), before)

    def test_handoff_does_not_start_dsh(self) -> None:
        root = make_handoff_root()
        self._commit_handoff(root)
        self.assertEqual(ctl.unresolved_intents(root), [])

    def test_handoff_last_job_runs_completion_validation(self) -> None:
        root = make_handoff_root(last=True)
        self._commit_handoff(root)
        self.assertEqual(ctl.read_json(root / "control" / "runtime.json")["project_status"], "COMPLETED")

    def test_handoff_recovers_from_each_file_replacement_crash_point(self) -> None:
        for replacement_count in range(5):
            with self.subTest(replacement_count=replacement_count):
                root = make_handoff_root()
                run = ctl.acquire_lease(root, "LUNA", "luna", None)
                lease = ctl.read_json(root / "control" / "run-lease.json")
                request = {"reason": "handoff", "patches": {}, "finish": {"outcome": "HANDOFF"}}
                updated, resolution = ctl.apply_request(root, lease, request)
                transaction = root / "control" / "transactions" / "99999999T999999999999Z-handoff"
                transaction.mkdir()
                prepare = {"transaction_id": transaction.name, "documents": updated, "intent_resolution": resolution}
                write_json(transaction / "prepare.json", prepare)
                for rel_path in sorted(updated)[:replacement_count]:
                    write_json(root / rel_path, updated[rel_path])
                (root / "control" / "run-lease.json").unlink()
                self.assertTrue(ctl.recover(root)["ok"])
                self.assertEqual(ctl.read_json(root / "control" / "runtime.json")["active_job_id"], "job-two")
                self.assertEqual(ctl.read_json(root / "jobs" / "job-one" / "state.json")["status"], "ARCHIVED")

    def test_completion_uses_project_contract_required_queue_order(self) -> None:
        root = make_handoff_root(last=True)
        documents = ctl.collect_documents(root)
        documents["control/runtime.json"]["active_job_id"] = None
        documents["control/queue.json"]["jobs"] = []
        runtime, queue, jobs = ctl.validate_all(root, documents)
        self.assertTrue(ctl.validate_project_completion(root, runtime, queue, jobs)["ok"])

    def test_completion_rejects_missing_required_job(self) -> None:
        root = make_handoff_root(last=True)
        project = ctl.read_json(root / "control" / "project-contract.json")
        project["required_queue_order"].append("missing")
        write_json(root / "control" / "project-contract.json", project)
        runtime = ctl.read_json(root / "control" / "runtime.json")
        runtime["active_job_id"] = None
        queue = ctl.read_json(root / "control" / "queue.json")
        queue["jobs"] = []
        jobs = {"job-one": ctl.read_json(root / "jobs" / "job-one" / "state.json")}
        result = ctl.validate_project_completion(root, runtime, queue, jobs)
        self.assertIn("REQUIRED_JOB_MISSING", {gap["code"] for gap in result["gaps"]})

    def test_protocol_cannot_claim_substitute_required_job(self) -> None:
        root = make_handoff_root(last=True)
        project = ctl.read_json(root / "control" / "project-contract.json")
        project["required_queue_order"] = ["missing-required"]
        write_json(root / "control" / "project-contract.json", project)
        protocol_path = root / "jobs" / "job-one" / "protocol-contract.json"
        protocol = ctl.read_json(protocol_path)
        protocol["substitutes_required_jobs"] = ["missing-required"]
        write_json(protocol_path, protocol)
        runtime = ctl.read_json(root / "control" / "runtime.json")
        runtime["active_job_id"] = None
        queue = ctl.read_json(root / "control" / "queue.json")
        queue["jobs"] = []
        jobs = {"job-one": ctl.read_json(root / "jobs" / "job-one" / "state.json")}
        result = ctl.validate_project_completion(root, runtime, queue, jobs)
        self.assertIn("REQUIRED_JOB_MISSING", {gap["code"] for gap in result["gaps"]})

    def test_completion_rejects_unresolved_intent(self) -> None:
        root = make_handoff_root(last=True)
        write_json(root / "control" / "intents" / "open.json", {"intent_id": "open"})
        runtime = ctl.read_json(root / "control" / "runtime.json")
        runtime["active_job_id"] = None
        queue = ctl.read_json(root / "control" / "queue.json")
        queue["jobs"] = []
        jobs = {"job-one": ctl.read_json(root / "jobs" / "job-one" / "state.json")}
        result = ctl.validate_project_completion(root, runtime, queue, jobs)
        self.assertIn("UNRESOLVED_INTENT", {gap["code"] for gap in result["gaps"]})

    def test_completion_rejects_open_event(self) -> None:
        root = make_handoff_root(last=True)
        runtime = ctl.read_json(root / "control" / "runtime.json")
        runtime["active_job_id"] = None
        queue = ctl.read_json(root / "control" / "queue.json")
        queue["jobs"] = []
        job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        job["controller"]["current_event"] = {
            "event_id": "job-one:1:BLOCKED",
            "generation": 1,
            "origin_status": "BLOCKED",
            "origin_semantic_revision": 1,
            "event_key": "sha256:" + "a" * 64,
            "root_cause_signature": "X",
            "lifecycle": "REQUIRED",
        }
        result = ctl.validate_project_completion(root, runtime, queue, {"job-one": job})
        self.assertIn("OPEN_TECHNICAL_EVENT", {gap["code"] for gap in result["gaps"]})

    def test_completion_rejects_open_user_gate(self) -> None:
        root = make_handoff_root(last=True)
        runtime = ctl.read_json(root / "control" / "runtime.json")
        runtime["active_job_id"] = None
        queue = ctl.read_json(root / "control" / "queue.json")
        queue["jobs"] = []
        job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        job["user_gate"] = {"gate_id": "gate-x", "gate_type": "PROJECT_OUTCOME_PHASE", "lifecycle": "REQUIRED", "requested_change_sha256": "x"}
        result = ctl.validate_project_completion(root, runtime, queue, {"job-one": job})
        self.assertIn("OPEN_USER_GATE", {gap["code"] for gap in result["gaps"]})

    def test_completion_accepts_full_control_plane_closure(self) -> None:
        root = make_handoff_root(last=True)
        runtime = ctl.read_json(root / "control" / "runtime.json")
        runtime["active_job_id"] = None
        queue = ctl.read_json(root / "control" / "queue.json")
        queue["jobs"] = []
        job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        self.assertTrue(ctl.validate_project_completion(root, runtime, queue, {"job-one": job})["ok"])

    def test_completion_rejects_active_or_unknown_controlled_session(self) -> None:
        root = make_handoff_root(last=True)
        job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        job["dsh"]["lifecycle_status"] = "unknown"
        runtime = ctl.read_json(root / "control" / "runtime.json")
        runtime["active_job_id"] = None
        queue = ctl.read_json(root / "control" / "queue.json")
        queue["jobs"] = []
        result = ctl.validate_project_completion(root, runtime, queue, {"job-one": job})
        self.assertIn("EXTERNAL_SESSION_STATE_UNVERIFIED", {gap["code"] for gap in result["gaps"]})


class MigrationTests(unittest.TestCase):
    def test_migration_refuses_unresolved_intent(self) -> None:
        root = make_legacy_root()
        write_json(root / "control" / "intents" / "x.json", {"intent_id": "x"})
        result = ctl.migrate_v81(root, dry_run=True)
        self.assertIn("MIGRATION_BLOCKED_UNRESOLVED_INTENT", {item["code"] for item in result["blockers"]})

    def test_migration_refuses_open_event(self) -> None:
        root = make_legacy_root(status="BLOCKED")
        result = ctl.migrate_v81(root, dry_run=True)
        self.assertIn("MIGRATION_BLOCKED_OPEN_EVENT", {item["code"] for item in result["blockers"]})

    def test_migration_refuses_other_live_lease(self) -> None:
        root = make_legacy_root()
        write_json(root / "control" / "run-lease.json", {"expires_at": "2999-01-01T00:00:00.000Z"})
        result = ctl.migrate_v81(root, dry_run=True)
        self.assertIn("MIGRATION_BLOCKED_LIVE_LEASE", {item["code"] for item in result["blockers"]})

    def test_migration_does_not_rewrite_historical_event_ids(self) -> None:
        root = make_legacy_root()
        old = ctl.read_json(root / "jobs" / "job-one" / "state.json")["controller"]["event_id"]
        result = ctl.migrate_v81(root)
        self.assertTrue(result["ok"])
        job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        self.assertIn(old, job["controller"]["legacy_event_ids"])

    def test_migration_preserves_legacy_verification_hash(self) -> None:
        root = make_legacy_root(status="ARCHIVED", active=False, queued=False)
        before = ctl.read_json(root / "jobs" / "job-one" / "state.json")["verification"]
        self.assertTrue(ctl.migrate_v81(root)["ok"])
        after = ctl.read_json(root / "jobs" / "job-one" / "state.json")["verification"]
        self.assertEqual(after["legacy_migration"]["pre_migration_verification_sha256"], ctl.digest_json(before))
        self.assertEqual(after["record_version"], "LEGACY_V8")

    def test_migration_maps_completed_lifecycle_status_to_terminal(self) -> None:
        root = make_legacy_root(status="ARCHIVED", active=False, queued=False)
        self.assertTrue(ctl.migrate_v81(root)["ok"])
        job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        self.assertEqual(ctl.dsh_lifecycle_class(job["dsh"]), "terminal")

    def test_legacy_required_job_remains_valid_after_migration(self) -> None:
        root = make_legacy_root(status="ARCHIVED", active=False, queued=False)
        self.assertTrue(ctl.migrate_v81(root)["ok"])
        documents = ctl.collect_documents(root)
        runtime, queue, jobs = ctl.validate_all(root, documents)
        self.assertTrue(ctl.validate_project_completion(root, runtime, queue, jobs)["ok"])


if __name__ == "__main__":
    unittest.main()
