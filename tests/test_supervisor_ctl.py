from __future__ import annotations

import copy
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from control import supervisor_ctl as ctl


NOW = "2026-09-02T00:00:00.000Z"


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(ctl.persisted_json_bytes(value))


def make_root(*, active: bool = True, queued: bool = True, job_status: str = "QUEUED") -> Path:
    root = Path(tempfile.mkdtemp())
    job_id = "job-one"
    specification = root / "jobs" / job_id / "specification.md"
    specification.parent.mkdir(parents=True)
    specification.write_text("# test specification\n", encoding="utf-8")

    project_contract = {
        "schema_version": 1,
        "contract_id": "test",
        "user_only_gates": sorted(ctl.VALID_USER_GATES),
        "required_queue_order": [job_id],
    }
    project_path = root / "control" / "project-contract.json"
    write_json(project_path, project_contract)
    project_hash = ctl.digest_file(project_path)

    protocol = {
        "schema_version": 1,
        "job_id": job_id,
        "project_contract_sha256": project_hash,
        "specification_sha256": ctl.digest_file(specification),
        "protocol_revision": 1,
        "protocol_authority_sha256": "a" * 64,
        "canonical_replay_a": "a",
        "canonical_replay_b": "b",
        "canonical_formal": "formal",
    }
    protocol_path = root / "jobs" / job_id / "protocol-contract.json"
    write_json(protocol_path, protocol)

    job = {
        "schema_version": ctl.JOB_SCHEMA,
        "job_id": job_id,
        "title": "test",
        "status": job_status,
        "created_at": NOW,
        "updated_at": NOW,
        "workspace": str(root / "workspace"),
        "baseline": {"git_commit": "abc"},
        "authorized_specification": {
            "current_sha256": protocol["specification_sha256"],
            "protocol_revision": 1,
            "protocol_authority_sha256": protocol["protocol_authority_sha256"],
            "canonical_replay_a": "a",
            "canonical_replay_b": "b",
            "canonical_formal": "formal",
        },
        "dsh": {
            "mode": "implement",
            "session_id": "session-one",
            "lifecycle_status": "completed",
            "epoch_count": 1,
            "auto_rework_count": 0,
            "last_checked_at": NOW,
            "last_result_summary": "ready",
            "continuation_required": True,
            "continuation_instruction": "continue exactly once",
        },
        "limits": {"max_auto_rework_epochs": 3, "max_auto_runtime_hours": 6},
        "runtime_tracking": {
            "started_at": None,
            "accumulated_seconds": 0,
            "last_progress_at": NOW,
            "legacy_runtime_unknown": False,
        },
        "controller": {
            "state_revision": 1,
            "semantic_revision": 1,
            "event_generation": 0,
            "current_event": None,
            "last_handled_event_id": None,
            "protocol_contract_sha256": ctl.digest_file(protocol_path),
            "escalation_metrics": {
                "total_dispatches": 0,
                "current_root_cause_signature": None,
                "root_causes": {},
            },
            "escalation": {},
        },
        "verification": {"commands": [], "results": [], "unverified_items": []},
        "decision": {"needs_user": False, "route": "CONTINUE_SAME_DSH_SESSION", "reason": None},
        "result": {"accepted_at": None, "summary": None, "changed_files": []},
        "user_gate": None,
    }
    if job_status in {"BLOCKED", "FAILED"}:
        job["decision"].update({"route": "SOL_ESCALATE", "error_code": "UNCLASSIFIED"})
        job["controller"]["event_generation"] = 1
        job["controller"]["current_event"] = {
            "event_id": f"{job_id}:1:{job_status}",
            "generation": 1,
            "origin_status": job_status,
            "origin_semantic_revision": 1,
            "event_key": ctl.technical_event_key(job, "UNCLASSIFIED"),
            "root_cause_signature": "UNCLASSIFIED",
            "resource_identities": [],
            "lifecycle": "REQUIRED",
            "created_at": NOW,
        }
    if job_status in {"ACCEPTED", "ARCHIVED"}:
        job["decision"].update({"route": "ACCEPT"})
        job["result"].update({"accepted_at": NOW, "summary": "passed"})
        job["verification"] = {
            "record_version": "V8_1",
            "provenance": "LUNA_ATTESTED",
            "review_run_id": "review-one",
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
    write_json(root / "jobs" / job_id / "state.json", job)

    queue = {
        "schema_version": ctl.QUEUE_SCHEMA,
        "queue_revision": 1,
        "updated_at": NOW,
        "planning_generation": 0,
        "last_empty_event": None,
        "jobs": [{
            "job_id": job_id,
            "priority": 1,
            "queued_at": NOW,
            "specification_sha256": protocol["specification_sha256"],
            "protocol_contract_sha256": ctl.digest_file(protocol_path),
        }] if queued else [],
    }
    runtime = {
        "schema_version": ctl.RUNTIME_SCHEMA,
        "updated_at": NOW,
        "project_status": "ACTIVE",
        "active_job_id": job_id if active else None,
        "controller": {
            "workflow_version": ctl.WORKFLOW_VERSION,
            "project_contract_sha256": project_hash,
            "user_only_gates": sorted(ctl.VALID_USER_GATES),
        },
        "defaults": {
            "max_parallel_jobs": 1,
            "check_interval_minutes": 45,
            "lease_ttl_minutes": 60,
            "routing_action_deadline_seconds": 75,
            "max_routing_heartbeat_wall_seconds": 90,
            "stale_after_minutes": 90,
            "max_auto_rework_epochs": 3,
            "max_auto_runtime_hours": 6,
            "max_sol_escalations_per_job": 5,
            "max_same_root_cause_escalations": 2,
        },
        "last_supervisor_run": None,
        "next_expected_run_at": None,
    }
    write_json(root / "control" / "queue.json", queue)
    write_json(root / "control" / "runtime.json", runtime)
    ctl.bootstrap(root)
    return root


class SupervisorControlTests(unittest.TestCase):
    def test_repository_template_matches_controller_schema(self) -> None:
        template_path = Path(__file__).resolve().parents[1] / "templates" / "job-state.json"
        template = json.loads(template_path.read_text(encoding="utf-8"))
        self.assertEqual(template["schema_version"], ctl.JOB_SCHEMA)
        self.assertIn("runtime_tracking", template)
        self.assertIn("protocol_contract_sha256", template["controller"])
        self.assertIn("escalation_metrics", template["controller"])

    def test_current_continuation_is_planned(self) -> None:
        root = make_root()
        result = ctl.verify(root)
        self.assertEqual(result["next_action"]["type"], "DSH_CONTINUE")
        self.assertEqual(result["next_action"]["session_id"], "session-one")

    def test_only_one_live_lease(self) -> None:
        root = make_root()
        first = ctl.acquire_lease(root, "LUNA", "thread-one", None)
        second = ctl.acquire_lease(root, "LUNA", "thread-two", None)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        self.assertEqual(second["action"]["reason"], "LEASE_HELD")

    def test_recover_sol_dispatch_binds_original_thread_atomically(self) -> None:
        for role in ("USER", "LUNA"):
            with self.subTest(role=role):
                root = make_root(job_status="BLOCKED")
                luna = ctl.acquire_lease(root, "LUNA", "thread-luna", None)
                event_id = luna["planned_action"]["event_id"]
                intent = ctl.prepare_intent(root, luna["lease_token"])["intent"]
                path = root / "request.json"
                write_json(path, {"expected": luna["expected"], "patches": {},
                                  "finish": {"outcome": "CONTROL_PLANE_RECOVERY_BLOCKED"}})
                ctl.commit_request(root, luna["lease_token"], path)
                recovery = ctl.acquire_lease(root, role, "recovery-thread", None)
                self.assertEqual(recovery["planned_action"]["type"], "RECOVER_INTENT")
                request = {
                    "job_id": "job-one", "expected": recovery["expected"], "patches": {},
                    "event_update": {"lifecycle": "DISPATCHED", "thread_id": "original-sol"},
                    "resolve_intent": {"intent_id": intent["intent_id"], "external_id": "wrong-thread",
                                       "outcome": "RECOVERED", "summary": "Verified original creation record"},
                    "finish": {"outcome": "SOL_DISPATCH_RECOVERED"},
                }
                write_json(path, request)
                with self.assertRaisesRegex(ctl.ControlError, "EVENT_RECOVERY_IDENTITY_MISMATCH"):
                    ctl.commit_request(root, recovery["lease_token"], path)
                request["resolve_intent"]["external_id"] = "original-sol"
                write_json(path, request)
                ctl.commit_request(root, recovery["lease_token"], path)
                job = ctl.read_json(root / "jobs/job-one/state.json")
                self.assertEqual(job["controller"]["current_event"]["event_id"], event_id)
                self.assertEqual(job["controller"]["semantic_revision"], 1)
                self.assertEqual(job["controller"]["escalation_metrics"]["total_dispatches"], 0)
                self.assertEqual(ctl.verify(root)["next_action"]["type"], "SOL_STATUS")
                sol = ctl.acquire_lease(root, "SOL", "original-sol", event_id)
                self.assertTrue(sol["ok"])

    def test_sol_dispatch_preserves_event_until_handler_acknowledges(self) -> None:
        root = make_root(job_status="BLOCKED")
        luna = ctl.acquire_lease(root, "LUNA", "thread-luna", None)
        event_id = luna["planned_action"]["event_id"]
        intent = ctl.prepare_intent(root, luna["lease_token"])["intent"]
        request = {
            "reason": "dispatch Sol",
            "job_id": "job-one",
            "expected": luna["expected"],
            "patches": {
                "job": {}
            },
            "event_update": {"lifecycle": "DISPATCHED", "thread_id": "sol-thread"},
            "resolve_intent": {
                "intent_id": intent["intent_id"],
                "outcome": "DISPATCHED",
                "external_id": "sol-thread",
            },
            "finish": {"outcome": "SOL_DISPATCHED"},
        }
        request_path = root / "dispatch.json"
        write_json(request_path, request)
        ctl.commit_request(root, luna["lease_token"], request_path)

        job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        self.assertEqual(job["controller"]["current_event"]["event_id"], event_id)
        self.assertEqual(job["controller"]["current_event"]["lifecycle"], "DISPATCHED")
        self.assertEqual(job["controller"]["state_revision"], 2)
        self.assertEqual(job["controller"]["semantic_revision"], 1)
        self.assertEqual(job["controller"]["escalation_metrics"]["total_dispatches"], 0)
        self.assertNotIn("acknowledged_at", job["controller"]["current_event"])
        next_action = ctl.verify(root)["next_action"]
        self.assertEqual(next_action["type"], "SOL_STATUS")
        self.assertEqual(next_action["thread_id"], "sol-thread")

        sol = ctl.acquire_lease(root, "SOL", "sol-thread", event_id)
        self.assertTrue(sol["ok"])
        self.assertEqual(sol["planned_action"]["type"], "SOL_HANDLE_EVENT")
        claim = {
            "reason": "Sol claimed event",
            "job_id": "job-one",
            "expected": sol["expected"],
            "patches": {"job": {}},
            "event_update": {"lifecycle": "ACKNOWLEDGED"},
            "retain_lease": True,
            "finish": {"outcome": "SOL_ACKNOWLEDGED"},
        }
        claim_path = root / "claim.json"
        write_json(claim_path, claim)
        claim_result = ctl.commit_request(root, sol["lease_token"], claim_path)
        claimed_job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        self.assertEqual(claimed_job["controller"]["current_event"]["lifecycle"], "ACKNOWLEDGED")
        self.assertEqual(claimed_job["controller"]["escalation_metrics"]["total_dispatches"], 0)
        self.assertTrue(claim_result["lease_retained"])
        resolution = {
            "reason": "Sol handled event",
            "job_id": "job-one",
            "expected": claim_result["expected"],
            "patches": {
                "job": {
                    "status": "REVIEW_PENDING",
                    "decision": {"route": "REVIEW", "reason": "fixed"},
                }
            },
            "finish": {"outcome": "SOL_RESOLVED"},
        }
        resolution_path = root / "resolution.json"
        write_json(resolution_path, resolution)
        ctl.commit_request(root, sol["lease_token"], resolution_path)

        job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        self.assertIsNotNone(job["controller"]["current_event"]["acknowledged_at"])
        self.assertEqual(job["controller"]["current_event"]["lifecycle"], "RESOLVED")
        self.assertEqual(job["controller"]["escalation_metrics"]["total_dispatches"], 1)
        self.assertEqual(job["controller"]["escalation_metrics"]["root_causes"]["UNCLASSIFIED"], 1)
        self.assertEqual(job["controller"]["last_handled_event_id"], event_id)
        runtime = ctl.read_json(root / "control" / "runtime.json")
        self.assertEqual(runtime["last_sol_event"]["event_id"], event_id)
        self.assertEqual(runtime["last_sol_event"]["thread_id"], "sol-thread")
        self.assertEqual(runtime["last_sol_event"]["outcome"], "SOL_RESOLVED")
        self.assertEqual(runtime["last_sol_event"]["job_id"], "job-one")
        self.assertEqual(runtime["last_sol_event"]["next_status"], "REVIEW_PENDING")

    def test_luna_does_not_redispatch_a_legacy_dispatched_event(self) -> None:
        root = make_root(job_status="BLOCKED")
        runtime = ctl.read_json(root / "control" / "runtime.json")
        queue = ctl.read_json(root / "control" / "queue.json")
        job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        dispatched_event = job["controller"]["current_event"]["event_id"]
        job["controller"]["state_revision"] = 2
        job["controller"]["current_event"]["lifecycle"] = "DISPATCHED"
        job["controller"]["current_event"]["thread_id"] = "sol-thread"
        action = ctl.determine_action(root, runtime, queue, {"job-one": job})
        self.assertEqual(action["type"], "SOL_STATUS")
        self.assertEqual(action["thread_id"], "sol-thread")

        sol_action = ctl.determine_sol_action(runtime, queue, {"job-one": job}, dispatched_event)
        self.assertEqual(sol_action["type"], "SOL_HANDLE_EVENT")

    def test_sol_can_wait_for_luna_lease_release(self) -> None:
        root = make_root(job_status="BLOCKED")
        luna = ctl.acquire_lease(root, "LUNA", "thread-luna", None)
        lease_path = root / "control" / "run-lease.json"

        def release_luna() -> None:
            time.sleep(0.05)
            lease_path.unlink()

        releaser = threading.Thread(target=release_luna)
        releaser.start()
        event_id = luna["planned_action"]["event_id"]
        # Model the committed dispatch that becomes visible immediately after
        # Luna releases its lease.
        job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        job["controller"]["state_revision"] += 1
        job["controller"]["current_event"]["lifecycle"] = "DISPATCHED"
        job["controller"]["current_event"]["thread_id"] = "sol-thread"
        write_json(root / "jobs" / "job-one" / "state.json", job)
        latest_dir, latest, _ = ctl.latest_transaction(root)
        latest["documents"]["jobs/job-one/state.json"] = job
        write_json(latest_dir / "prepare.json", latest)
        commit = ctl.read_json(latest_dir / "commit.json")
        commit["document_hashes"] = ctl.document_hashes(latest["documents"])
        write_json(latest_dir / "commit.json", commit)

        sol = ctl.acquire_lease(root, "SOL", "sol-thread", event_id, wait_lease_seconds=1)
        releaser.join()
        self.assertTrue(sol["ok"])
        self.assertEqual(sol["planned_action"]["type"], "SOL_HANDLE_EVENT")

    def test_begin_cli_accepts_wait_lease_seconds(self) -> None:
        args = ctl.build_parser().parse_args(
            ["begin", "--role", "SOL", "--event-id", "job-one:1:BLOCKED", "--wait-lease-seconds", "30"]
        )
        self.assertEqual(args.wait_lease_seconds, 30.0)

    def test_sol_rejects_a_different_visible_thread(self) -> None:
        root = make_root(job_status="BLOCKED")
        luna = ctl.acquire_lease(root, "LUNA", "thread-luna", None)
        request_path = root / "dispatch.json"
        write_json(
            request_path,
            {
                "job_id": "job-one",
                "expected": luna["expected"],
                "patches": {"job": {}},
                "event_update": {"lifecycle": "DISPATCHED", "thread_id": "sol-thread"},
                "finish": {"outcome": "SOL_DISPATCHED"},
            },
        )
        ctl.commit_request(root, luna["lease_token"], request_path)
        event_id = ctl.read_json(root / "jobs" / "job-one" / "state.json")["controller"]["current_event"]["event_id"]

        wrong = ctl.acquire_lease(root, "SOL", "other-thread", event_id)
        self.assertFalse(wrong["ok"])
        self.assertEqual(wrong["action"]["reason"], "SOL_THREAD_ID_MISMATCH")

        intended = ctl.acquire_lease(root, "SOL", None, event_id)
        self.assertTrue(intended["ok"])
        self.assertEqual(intended["source_thread_id"], "sol-thread")

    def test_dispatch_requires_a_visible_thread_id(self) -> None:
        root = make_root(job_status="BLOCKED")
        luna = ctl.acquire_lease(root, "LUNA", "thread-luna", None)
        request_path = root / "dispatch.json"
        write_json(
            request_path,
            {
                "job_id": "job-one",
                "expected": luna["expected"],
                "patches": {"job": {}},
                "event_update": {"lifecycle": "DISPATCHED", "thread_id": None},
                "finish": {"outcome": "SOL_DISPATCHED"},
            },
        )
        with self.assertRaisesRegex(ctl.ControlError, "SOL_DISPATCH_REQUIRES_THREAD_ID"):
            ctl.commit_request(root, luna["lease_token"], request_path)

    def test_sol_status_observation_throttles_rechecks(self) -> None:
        root = make_root(job_status="BLOCKED")
        luna = ctl.acquire_lease(root, "LUNA", "thread-luna", None)
        request_path = root / "dispatch.json"
        write_json(
            request_path,
            {
                "job_id": "job-one",
                "expected": luna["expected"],
                "patches": {"job": {}},
                "event_update": {"lifecycle": "DISPATCHED", "thread_id": "sol-thread"},
                "finish": {"outcome": "SOL_DISPATCHED"},
            },
        )
        ctl.commit_request(root, luna["lease_token"], request_path)
        status_run = ctl.acquire_lease(root, "LUNA", "thread-luna", None)
        self.assertEqual(status_run["planned_action"]["type"], "SOL_STATUS")
        write_json(
            request_path,
            {
                "job_id": "job-one",
                "expected": status_run["expected"],
                "patches": {"job": {}},
                "event_update": {"thread_status": "active"},
                "finish": {"outcome": "SOL_RUNNING"},
            },
        )
        result = ctl.commit_request(root, status_run["lease_token"], request_path)
        self.assertEqual(result["report"]["next_action"], "NO_ACTION")
        self.assertEqual(ctl.verify(root)["next_action"]["reason"], "SOL_THREAD_RECENTLY_CHECKED")

    def test_renew_extends_a_sol_lease(self) -> None:
        root = make_root(job_status="BLOCKED")
        luna = ctl.acquire_lease(root, "LUNA", "thread-luna", None)
        request_path = root / "dispatch.json"
        write_json(
            request_path,
            {
                "job_id": "job-one",
                "expected": luna["expected"],
                "patches": {"job": {}},
                "event_update": {"lifecycle": "DISPATCHED", "thread_id": "sol-thread"},
                "finish": {"outcome": "SOL_DISPATCHED"},
            },
        )
        ctl.commit_request(root, luna["lease_token"], request_path)
        event_id = ctl.read_json(root / "jobs" / "job-one" / "state.json")["controller"]["current_event"]["event_id"]
        sol = ctl.acquire_lease(root, "SOL", "sol-thread", event_id)
        before = ctl.parse_time(sol["expires_at"])
        renewed = ctl.renew_lease(root, sol["lease_token"])
        self.assertGreaterEqual(ctl.parse_time(renewed["expires_at"]), before)

    def test_staged_protocol_adoption_updates_files_state_and_queue(self) -> None:
        root = make_root(job_status="BLOCKED")
        luna = ctl.acquire_lease(root, "LUNA", "thread-luna", None)
        request_path = root / "dispatch.json"
        write_json(
            request_path,
            {
                "job_id": "job-one",
                "expected": luna["expected"],
                "patches": {"job": {}},
                "event_update": {"lifecycle": "DISPATCHED", "thread_id": "sol-thread"},
                "finish": {"outcome": "SOL_DISPATCHED"},
            },
        )
        ctl.commit_request(root, luna["lease_token"], request_path)
        job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        event_id = job["controller"]["current_event"]["event_id"]
        history = root / "jobs" / "job-one" / "history" / "protocol-revision-1"
        history.mkdir(parents=True)
        shutil.copyfile(root / "jobs" / "job-one" / "specification.md", history / "specification.md")
        shutil.copyfile(root / "jobs" / "job-one" / "protocol-contract.json", history / "protocol-contract.json")
        staged = root / "control" / "staging" / "test-r2"
        staged.mkdir(parents=True)
        staged_spec = staged / "specification.md"
        staged_spec.write_text("# revised specification\n", encoding="utf-8")
        old_contract_hash = ctl.digest_file(history / "protocol-contract.json")
        protocol = ctl.read_json(history / "protocol-contract.json")
        protocol.update(
            {
                "specification_sha256": ctl.digest_file(staged_spec),
                "protocol_revision": 2,
                "authorized_at": ctl.iso_utc(),
                "authorized_by_event_id": event_id,
                "preserved_protocol_revisions": [
                    {
                        "protocol_revision": 1,
                        "specification": "jobs/job-one/history/protocol-revision-1/specification.md",
                        "specification_sha256": ctl.digest_file(history / "specification.md"),
                        "protocol_contract": "jobs/job-one/history/protocol-revision-1/protocol-contract.json",
                        "protocol_contract_sha256": old_contract_hash,
                    }
                ],
            }
        )
        staged_contract = staged / "protocol-contract.json"
        write_json(staged_contract, protocol)
        sol = ctl.acquire_lease(root, "SOL", "sol-thread", event_id)
        write_json(
            request_path,
            {
                "job_id": "job-one",
                "expected": sol["expected"],
                "adopt_protocol_contract": True,
                "protocol_staging": {
                    "specification": "control/staging/test-r2/specification.md",
                    "protocol_contract": "control/staging/test-r2/protocol-contract.json",
                },
                "patches": {"job": {"status": "REVIEW_PENDING", "decision": {"route": "REVIEW"}}},
                "finish": {"outcome": "SOL_RESOLVED"},
            },
        )
        result = ctl.commit_request(root, sol["lease_token"], request_path)
        committed_job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        queue = ctl.read_json(root / "control" / "queue.json")
        self.assertEqual(committed_job["authorized_specification"]["protocol_revision"], 2)
        self.assertEqual(committed_job["authorized_specification"]["current_sha256"], ctl.digest_file(staged_spec))
        self.assertEqual(queue["jobs"][0]["specification_sha256"], ctl.digest_file(staged_spec))
        self.assertEqual(queue["jobs"][0]["protocol_contract_sha256"], ctl.digest_file(staged_contract))
        transaction = ctl.read_json(root / "control" / "transactions" / result["transaction_id"] / "prepare.json")
        self.assertEqual(set(transaction["file_updates"]), {"jobs/job-one/specification.md", "jobs/job-one/protocol-contract.json"})

    def test_intent_is_idempotent_and_commit_resolves_it(self) -> None:
        root = make_root()
        run = ctl.acquire_lease(root, "LUNA", "thread-one", None)
        first = ctl.prepare_intent(root, run["lease_token"])
        second = ctl.prepare_intent(root, run["lease_token"])
        self.assertEqual(first["intent"]["intent_id"], second["intent"]["intent_id"])

        request = {
            "reason": "DSH_CONTINUE_ACCEPTED",
            "job_id": "job-one",
            "expected": {"job_state_revision": 1, "job_status": "QUEUED", "queue_revision": 1},
            "patches": {
                "job": {
                    "status": "RUNNING",
                    "dsh": {"lifecycle_status": "running", "continuation_required": False},
                }
            },
            "resolve_intent": {
                "intent_id": first["intent"]["intent_id"],
                "outcome": "RUNNING",
                "external_id": "session-one",
            },
            "finish": {"outcome": "DSH_CONTINUED_RUNNING"},
        }
        request_path = root / "request.json"
        write_json(request_path, request)
        result = ctl.commit_request(root, run["lease_token"], request_path)

        job = ctl.read_json(root / "jobs" / "job-one" / "state.json")
        self.assertEqual(job["status"], "RUNNING")
        self.assertEqual(job["controller"]["state_revision"], 2)
        self.assertIsNotNone(job["runtime_tracking"]["started_at"])
        self.assertFalse((root / "control" / "run-lease.json").exists())
        self.assertEqual(ctl.verify(root)["next_action"]["type"], "DSH_STATUS")
        self.assertEqual(result["report"]["visibility"], "PROGRESS")
        self.assertEqual(result["report"]["status_before"], "QUEUED")
        self.assertEqual(result["report"]["status_after"], "RUNNING")
        self.assertEqual(result["report"]["next_action"], "DSH_STATUS")
        self.assertFalse(result["report"]["needs_user"])

    def test_no_action_commit_is_silent(self) -> None:
        root = make_root(active=False, queued=False, job_status="ARCHIVED")
        runtime_path = root / "control" / "runtime.json"
        runtime = ctl.read_json(runtime_path)
        runtime["project_status"] = "PAUSED"
        write_json(runtime_path, runtime)
        latest_dir, latest_prepare, _ = ctl.latest_transaction(root)
        latest_prepare["documents"]["control/runtime.json"] = runtime
        write_json(latest_dir / "prepare.json", latest_prepare)
        commit = ctl.read_json(latest_dir / "commit.json")
        commit["document_hashes"] = ctl.document_hashes(latest_prepare["documents"])
        write_json(latest_dir / "commit.json", commit)

        run = ctl.acquire_lease(root, "LUNA", "thread-one", None)
        request = {
            "reason": "NO_ACTION",
            "patches": {},
            "finish": {"outcome": "NO_ACTION"},
        }
        request_path = root / "no-action.json"
        write_json(request_path, request)
        result = ctl.commit_request(root, run["lease_token"], request_path)
        self.assertEqual(result["report"]["visibility"], "SILENT")

    def test_overlong_routing_commit_requires_attention(self) -> None:
        root = make_root()
        run = ctl.acquire_lease(root, "LUNA", "thread-one", None)
        lease_path = root / "control" / "run-lease.json"
        lease = ctl.read_json(lease_path)
        lease["started_at"] = NOW
        lease["expires_at"] = "2099-01-01T00:00:00.000Z"
        write_json(lease_path, lease)
        request_path = root / "request.json"
        write_json(
            request_path,
            {
                "job_id": "job-one",
                "expected": run["expected"],
                "patches": {},
                "finish": {"outcome": "DSH_CONTINUED"},
            },
        )
        result = ctl.commit_request(root, run["lease_token"], request_path)
        self.assertEqual(result["report"]["visibility"], "ATTENTION")
        self.assertEqual(result["report"]["policy_violations"], ["ROUTING_HEARTBEAT_WALL_EXCEEDED"])

    def test_blocked_commit_requires_attention(self) -> None:
        root = make_root(job_status="REVIEW_PENDING")
        run = ctl.acquire_lease(root, "LUNA", "thread-one", None)
        request = {
            "reason": "evidence conflict",
            "job_id": "job-one",
            "patches": {
                "job": {
                    "status": "BLOCKED",
                    "decision": {"needs_user": False, "route": "SOL_ESCALATE", "reason": "conflict", "error_code": "EVIDENCE_CONFLICT"},
                    "controller": {
                        "escalation_metrics": {"current_root_cause_signature": "EVIDENCE_CONFLICT"},
                    },
                }
            },
            "finish": {"outcome": "REVIEW_BLOCKED_EVIDENCE_CONFLICT"},
        }
        request_path = root / "blocked.json"
        write_json(request_path, request)
        result = ctl.commit_request(root, run["lease_token"], request_path)
        self.assertEqual(result["report"]["visibility"], "ATTENTION")
        self.assertEqual(result["report"]["status_after"], "BLOCKED")
        self.assertEqual(result["report"]["next_action"], "SOL_ESCALATE")

    def test_accepted_commit_is_terminal_visibility(self) -> None:
        root = make_root(job_status="REVIEW_PENDING")
        run = ctl.acquire_lease(root, "LUNA", "thread-one", None)
        request = {
            "reason": "all checks passed",
            "job_id": "job-one",
            "patches": {
                "job": {
                    "status": "ACCEPTED",
                    "verification": {
                        "record_version": "V8_1",
                        "provenance": "LUNA_ATTESTED",
                        "review_run_id": "review-two",
                        "specification_sha256": ctl.read_json(root / "jobs" / "job-one" / "protocol-contract.json")["specification_sha256"],
                        "protocol_contract_sha256": ctl.digest_file(root / "jobs" / "job-one" / "protocol-contract.json"),
                        "started_at": NOW,
                        "completed_at": NOW,
                        "result": "PASSED",
                        "commands": [{"command_id": "verify-001", "command": "true", "exit_code": 0, "key_output": "ok", "evidence_paths": [], "evidence_sha256": []}],
                        "unverified_items": [],
                    },
                    "decision": {"needs_user": False, "route": "ACCEPT", "reason": "passed"},
                    "result": {"accepted_at": NOW, "summary": "passed", "changed_files": []},
                }
            },
            "finish": {"outcome": "ACCEPTED", "summary": "独立验收通过。"},
        }
        request_path = root / "accepted.json"
        write_json(request_path, request)
        result = ctl.commit_request(root, run["lease_token"], request_path)
        self.assertEqual(result["report"]["visibility"], "TERMINAL")
        self.assertEqual(result["report"]["summary"], "独立验收通过。")
        self.assertEqual(result["report"]["next_action"], "HANDOFF_ACCEPTED_JOB")

    def test_existing_session_cannot_be_replaced(self) -> None:
        root = make_root()
        run = ctl.acquire_lease(root, "LUNA", "thread-one", None)
        request = {
            "reason": "bad replacement",
            "job_id": "job-one",
            "patches": {"job": {"dsh": {"session_id": "session-two"}}},
            "finish": {"outcome": "BAD"},
        }
        request_path = root / "request.json"
        write_json(request_path, request)
        with self.assertRaisesRegex(ctl.ControlError, "cannot be replaced"):
            ctl.commit_request(root, run["lease_token"], request_path)

    def test_invalid_user_gate_is_rejected(self) -> None:
        root = make_root()
        run = ctl.acquire_lease(root, "LUNA", "thread-one", None)
        request = {
            "reason": "bad gate",
            "job_id": "job-one",
            "patches": {"job": {"decision": {"needs_user": True, "user_only_gate": "TECHNICAL_CHOICE"}}},
            "finish": {"outcome": "BAD"},
        }
        request_path = root / "request.json"
        write_json(request_path, request)
        with self.assertRaisesRegex(ctl.ControlError, "USER_GATE_REQUIRES_BOUND_SOL_EVENT"):
            ctl.commit_request(root, run["lease_token"], request_path)

    def test_queue_empty_cannot_resolve_without_postcondition(self) -> None:
        root = make_root(active=False, queued=False)
        run = ctl.acquire_lease(root, "LUNA", "thread-one", None)
        self.assertEqual(run["planned_action"]["type"], "SOL_QUEUE_EMPTY")
        intent = ctl.prepare_intent(root, run["lease_token"])["intent"]
        event_id = run["planned_action"]["event_id"]
        dispatch = {
            "reason": "queue-empty Sol dispatched",
            "patches": {},
            "queue_event_update": {"lifecycle": "DISPATCHED", "thread_id": "sol-thread"},
            "resolve_intent": {"intent_id": intent["intent_id"], "outcome": "DISPATCHED", "external_id": "sol-thread"},
            "finish": {"outcome": "QUEUE_EMPTY_SOL_DISPATCHED"},
        }
        request_path = root / "dispatch.json"
        write_json(request_path, dispatch)
        ctl.commit_request(root, run["lease_token"], request_path)

        sol_run = ctl.acquire_lease(root, "SOL", "sol-thread", event_id)
        self.assertEqual(sol_run["planned_action"]["type"], "SOL_HANDLE_QUEUE_EMPTY")
        request = {
            "reason": "empty result",
            "patches": {},
            "finish": {"outcome": "QUEUE_EMPTY_RESOLVED"},
        }
        request_path = root / "sol-result.json"
        write_json(request_path, request)
        with self.assertRaisesRegex(ctl.ControlError, "adding a job or completing"):
            ctl.commit_request(root, sol_run["lease_token"], request_path)

    def test_prepared_transaction_rolls_forward_after_crash(self) -> None:
        root = make_root()
        documents = ctl.collect_documents(root)
        after = copy.deepcopy(documents)
        after["control/runtime.json"]["next_expected_run_at"] = "2030-01-01T00:00:00.000Z"
        transaction_dir = root / "control" / "transactions" / "99999999T999999999999Z-crash"
        transaction_dir.mkdir()
        prepare = {
            "schema_version": 1,
            "transaction_id": transaction_dir.name,
            "run_id": "crashed",
            "actor": "LUNA",
            "reason": "test",
            "created_at": NOW,
            "before_hashes": ctl.document_hashes(documents),
            "documents": after,
            "intent_resolution": None,
        }
        write_json(transaction_dir / "prepare.json", prepare)
        recovered = ctl.recover_transactions(root)
        self.assertIn(transaction_dir.name, recovered)
        self.assertTrue((transaction_dir / "commit.json").exists())
        self.assertEqual(ctl.read_json(root / "control" / "runtime.json")["next_expected_run_at"], "2030-01-01T00:00:00.000Z")

    def test_prepared_transaction_rolls_forward_file_updates_after_crash(self) -> None:
        root = make_root()
        transaction = root / "control" / "transactions" / "99999999T999999999999Z-file-update"
        transaction.mkdir()
        data = b"recoverable protocol payload\n"
        write_json(
            transaction / "prepare.json",
            {
                "schema_version": 1,
                "transaction_id": transaction.name,
                "documents": ctl.collect_documents(root),
                "file_updates": {
                    "control/staging/recovered.txt": {
                        "encoding": "base64",
                        "data": ctl.base64.b64encode(data).decode("ascii"),
                        "sha256": ctl.digest_bytes(data),
                    }
                },
                "intent_resolution": None,
            },
        )
        self.assertTrue(ctl.recover(root)["ok"])
        self.assertEqual((root / "control" / "staging" / "recovered.txt").read_bytes(), data)
        commit = ctl.read_json(transaction / "commit.json")
        self.assertEqual(commit["file_update_hashes"]["control/staging/recovered.txt"], ctl.digest_bytes(data))
        self.assertTrue(ctl.verify(root)["ok"])

    def test_direct_state_edit_is_detected(self) -> None:
        root = make_root()
        runtime_path = root / "control" / "runtime.json"
        runtime = ctl.read_json(runtime_path)
        runtime["project_status"] = "FAILED"
        write_json(runtime_path, runtime)
        self.assertEqual(ctl.verify(root)["status"], "STATE_DRIFT")

    def test_direct_project_contract_edit_is_detected(self) -> None:
        root = make_root()
        contract_path = root / "control" / "project-contract.json"
        contract = ctl.read_json(contract_path)
        contract["contract_id"] = "changed-outside-controller"
        write_json(contract_path, contract)
        self.assertEqual(ctl.verify(root)["status"], "STATE_DRIFT")

    def test_user_can_migrate_contract_and_seed_multiple_jobs(self) -> None:
        root = make_root(active=False, queued=False, job_status="ARCHIVED")
        old_contract_hash = ctl.digest_file(root / "control" / "project-contract.json")
        contract = {
            "schema_version": 1,
            "contract_id": "full-project",
            "supersedes_contract_sha256": [old_contract_hash],
            "authorization": {"basis": "explicit user request"},
            "user_only_gates": sorted(ctl.VALID_USER_GATES),
        }
        project_hash = ctl.digest_persisted_json(contract)

        new_jobs = []
        queue_entries = []
        for index, job_id in enumerate(("job-two", "job-three"), start=1):
            specification = root / "jobs" / job_id / "specification.md"
            specification.parent.mkdir(parents=True)
            specification.write_text(f"# {job_id}\n", encoding="utf-8")
            spec_hash = ctl.digest_file(specification)
            authority_hash = str(index) * 64
            protocol = {
                "schema_version": 1,
                "job_id": job_id,
                "project_contract_sha256": project_hash,
                "specification_sha256": spec_hash,
                "protocol_revision": 1,
                "protocol_authority_sha256": authority_hash,
                "canonical_replay_a": None,
                "canonical_replay_b": None,
                "canonical_formal": f"results/{job_id}",
            }
            protocol_path = root / "jobs" / job_id / "protocol-contract.json"
            write_json(protocol_path, protocol)
            state = {
                "schema_version": ctl.JOB_SCHEMA,
                "job_id": job_id,
                "title": job_id,
                "status": "DRAFT",
                "created_at": NOW,
                "updated_at": NOW,
                "workspace": str(root / "workspace"),
                "baseline": {"git_commit": "abc"},
                "authorized_specification": {
                    "initial_baseline_sha256": spec_hash,
                    "current_sha256": spec_hash,
                    "authorized_at": NOW,
                    "authorized_by_event_id": "USER:test",
                    "protocol_revision": 1,
                    "protocol_authority_sha256": authority_hash,
                    "canonical_replay_a": None,
                    "canonical_replay_b": None,
                    "canonical_formal": f"results/{job_id}",
                },
                "dsh": {
                    "mode": "implement",
                    "session_id": None,
                    "lifecycle_status": None,
                    "epoch_count": 0,
                    "auto_rework_count": 0,
                    "last_checked_at": None,
                    "last_result_summary": None,
                    "continuation_required": False,
                    "continuation_instruction": None,
                },
                "limits": {"max_auto_rework_epochs": 3, "max_auto_runtime_hours": 6},
                "runtime_tracking": {
                    "started_at": None,
                    "accumulated_seconds": 0,
                    "last_progress_at": None,
                    "progress_fingerprint": None,
                    "legacy_runtime_unknown": False,
                },
                "controller": {
                    "state_revision": 0,
                    "event_id": f"{job_id}:0:DRAFT",
                    "last_handled_event_id": None,
                    "protocol_contract_sha256": None,
                    "escalation_metrics": {
                        "total_dispatches": 0,
                        "current_root_cause_signature": None,
                        "root_causes": {},
                    },
                    "escalation": {},
                },
                "verification": {"commands": [], "last_run_at": None, "results": [], "unverified_items": []},
                "decision": {"needs_user": False, "user_only_gate": None, "route": None, "reason": None},
                "result": {"accepted_at": None, "summary": None, "changed_files": []},
            }
            new_jobs.append({"job_id": job_id, "state": state})
            queue_entries.append({"job_id": job_id, "priority": index * 100, "queued_at": NOW})

        run = ctl.acquire_lease(root, "USER", "thread-user", None)
        request = {
            "reason": "USER_AUTHORIZED_FULL_PROJECT_QUEUE",
            "project_contract": contract,
            "new_jobs": new_jobs,
            "patches": {
                "runtime": {"project_status": "ACTIVE", "active_job_id": None},
                "queue": {"jobs": queue_entries},
            },
            "finish": {"outcome": "PROJECT_SCOPE_MIGRATED_QUEUE_SEEDED"},
        }
        request_path = root / "request.json"
        write_json(request_path, request)
        ctl.commit_request(root, run["lease_token"], request_path)

        runtime = ctl.read_json(root / "control" / "runtime.json")
        queue = ctl.read_json(root / "control" / "queue.json")
        self.assertEqual(runtime["controller"]["project_contract_sha256"], project_hash)
        self.assertEqual([item["job_id"] for item in queue["jobs"]], ["job-two", "job-three"])
        self.assertEqual(ctl.verify(root)["next_action"], {"type": "ACTIVATE_JOB", "job_id": "job-two"})

    def test_user_can_atomically_migrate_existing_queued_protocols(self) -> None:
        root = make_root(active=False, queued=True, job_status="QUEUED")
        run = ctl.acquire_lease(root, "USER", "thread-user", None)
        job_id = "job-one"
        job_path = root / "jobs" / job_id
        specification_path = job_path / "specification.md"
        protocol_path = job_path / "protocol-contract.json"
        old_specification_hash = ctl.digest_file(specification_path)
        old_contract_hash = ctl.digest_file(protocol_path)
        history = job_path / "history" / "protocol-revision-1"
        history.mkdir(parents=True)
        (history / "specification.md").write_bytes(specification_path.read_bytes())
        (history / "protocol-contract.json").write_bytes(protocol_path.read_bytes())

        specification_path.write_text("# test specification\n\nLayer binding required.\n", encoding="utf-8")
        protocol = ctl.read_json(protocol_path)
        protocol.update(
            {
                "specification_sha256": ctl.digest_file(specification_path),
                "authorized_at": NOW,
                "authorized_by_event_id": "USER:test:protocol-migration",
                "protocol_revision": 2,
                "canonical_layer_binding": "artifacts/job-one/binding/layer-binding.json",
                "canonical_evidence_baseline": "artifacts/job-one/binding/original-evidence-baseline.json",
                "predecessors": ["producer-one"],
                "preserved_protocol_revisions": [
                    {
                        "protocol_revision": 1,
                        "specification": "jobs/job-one/history/protocol-revision-1/specification.md",
                        "specification_sha256": old_specification_hash,
                        "protocol_contract": "jobs/job-one/history/protocol-revision-1/protocol-contract.json",
                        "protocol_contract_sha256": old_contract_hash,
                    }
                ],
            }
        )
        write_json(protocol_path, protocol)
        request = {
            "reason": "migrate queued protocol",
            "protocol_migrations": [{"job_id": job_id}],
            "patches": {},
            "finish": {"outcome": "PROTOCOLS_MIGRATED"},
        }
        request_path = root / "request.json"
        write_json(request_path, request)
        ctl.commit_request(root, run["lease_token"], request_path)

        job = ctl.read_json(job_path / "state.json")
        queue = ctl.read_json(root / "control" / "queue.json")
        self.assertEqual(job["authorized_specification"]["protocol_revision"], 2)
        self.assertEqual(job["authorized_specification"]["current_sha256"], protocol["specification_sha256"])
        self.assertEqual(job["controller"]["state_revision"], 2)
        self.assertEqual(job["controller"]["semantic_revision"], 2)
        self.assertEqual(job["controller"]["protocol_contract_sha256"], ctl.digest_file(protocol_path))
        self.assertEqual(queue["queue_revision"], 2)
        self.assertEqual(queue["planning_generation"], 1)
        self.assertEqual(queue["jobs"][0]["specification_sha256"], protocol["specification_sha256"])
        self.assertEqual(queue["jobs"][0]["protocol_contract_sha256"], ctl.digest_file(protocol_path))
        self.assertEqual(ctl.verify(root)["next_action"], {"type": "ACTIVATE_JOB", "job_id": "job-one"})

    def test_user_can_reconcile_stale_queue_authority_hashes(self) -> None:
        root = make_root(job_status="REVIEW_PENDING")
        queue_path = root / "control" / "queue.json"
        queue = ctl.read_json(queue_path)
        queue["jobs"][0]["specification_sha256"] = "0" * 64
        queue["jobs"][0]["protocol_contract_sha256"] = "1" * 64
        write_json(queue_path, queue)
        latest_dir, latest, _ = ctl.latest_transaction(root)
        latest["documents"]["control/queue.json"] = queue
        write_json(latest_dir / "prepare.json", latest)
        commit = ctl.read_json(latest_dir / "commit.json")
        commit["document_hashes"] = ctl.document_hashes(latest["documents"])
        write_json(latest_dir / "commit.json", commit)

        run = ctl.acquire_lease(root, "USER", "user-thread", None)
        request_path = root / "reconcile.json"
        write_json(
            request_path,
            {
                "job_id": "job-one",
                "expected": run["expected"],
                "reconcile_queue_authority": True,
                "patches": {},
                "finish": {"outcome": "QUEUE_AUTHORITY_RECONCILED"},
            },
        )
        ctl.commit_request(root, run["lease_token"], request_path)
        reconciled = ctl.read_json(queue_path)["jobs"][0]
        self.assertEqual(reconciled["specification_sha256"], ctl.digest_file(root / "jobs" / "job-one" / "specification.md"))
        self.assertEqual(reconciled["protocol_contract_sha256"], ctl.digest_file(root / "jobs" / "job-one" / "protocol-contract.json"))

    def test_luna_cannot_batch_migrate_existing_protocols(self) -> None:
        root = make_root(active=False, queued=True, job_status="QUEUED")
        run = ctl.acquire_lease(root, "LUNA", "thread-luna", None)
        request = {
            "reason": "bad",
            "protocol_migrations": [{"job_id": "job-one"}],
            "patches": {},
            "finish": {"outcome": "BAD"},
        }
        request_path = root / "request.json"
        write_json(request_path, request)
        with self.assertRaisesRegex(ctl.ControlError, "USER or MIGRATION"):
            ctl.commit_request(root, run["lease_token"], request_path)

    def test_luna_cannot_batch_seed_jobs(self) -> None:
        root = make_root(active=False, queued=False, job_status="ARCHIVED")
        run = ctl.acquire_lease(root, "LUNA", "thread-luna", None)
        request = {"reason": "bad", "new_jobs": [], "patches": {}, "finish": {"outcome": "BAD"}}
        request_path = root / "request.json"
        write_json(request_path, request)
        with self.assertRaisesRegex(ctl.ControlError, "USER or MIGRATION"):
            ctl.commit_request(root, run["lease_token"], request_path)


if __name__ == "__main__":
    unittest.main()
