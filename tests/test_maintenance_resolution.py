import copy
import unittest

from control import supervisor_ctl as ctl
from tests.test_supervisor_ctl import make_root, write_json
from tests.test_workflow_v81 import rewrite_committed_head


class MaintenanceTests(unittest.TestCase):
    def setup_case(self, role="USER"):
        root = make_root(job_status="FAILED")
        docs = ctl.collect_documents(root)
        runtime = docs["control/runtime.json"]
        runtime["project_status"] = "PAUSED"
        job = docs["jobs/job-one/state.json"]
        event = job["controller"]["current_event"]
        event.update(lifecycle="ACKNOWLEDGED", thread_id="original-sol", acknowledged_at=runtime["updated_at"])
        metrics = job["controller"]["escalation_metrics"]
        metrics.update(total_dispatches=2, current_root_cause_signature=None,
                       root_causes={event["root_cause_signature"]: 2})
        runtime["defaults"]["max_same_root_cause_escalations"] = 2
        job["dsh"].update(session_id="original-session", lifecycle_status="failed")
        rewrite_committed_head(root, docs)
        evidence_path = root / "control/diagnostics/repair.json"
        write_json(evidence_path, {"verified": True})
        run = ctl.acquire_lease(root, role, "original-sol" if role == "SOL" else None,
                                event["event_id"] if role == "SOL" else None)
        request = {
            "reason": "Explicit user requested recovery", "job_id": "job-one", "expected": run["expected"],
            "maintenance_resolution": {
                "event_id": event["event_id"], "thread_id": "original-sol",
                "authorization_basis": "User requested recovery",
                "evidence": [{"path": "control/diagnostics/repair.json", "sha256": ctl.digest_file(evidence_path)}],
            },
            "patches": {"runtime": {"project_status": "ACTIVE"}, "job": {
                "status": "QUEUED", "dsh": {"continuation_required": True, "continuation_instruction": "Continue original session"},
                "decision": {"needs_user": False, "route": "CONTINUE_SAME_DSH_SESSION"}}},
            "finish": {"outcome": "USER_MAINTENANCE_RECOVERED", "summary": "Recovered"},
        }
        return root, run, request, copy.deepcopy(job)

    def commit(self, root, run, request):
        path = root / "maintenance.json"
        write_json(path, request)
        return ctl.commit_request(root, run["lease_token"], path)

    def test_null_legacy_signature_cannot_hide_exhausted_event_budget(self):
        root, run, request, job = self.setup_case()
        runtime = ctl.read_json(root / "control/runtime.json")
        self.assertEqual(ctl.escalation_allowed(runtime, job), (False, "SAME_ROOT_CAUSE_BUDGET_EXHAUSTED"))

    def test_user_repair_requeues_same_session_preserving_counters_and_identity(self):
        root, run, request, old = self.setup_case()
        result = self.commit(root, run, request)
        new = ctl.read_json(root / "jobs/job-one/state.json")
        self.assertEqual(result["report"]["next_action"], "DSH_CONTINUE")
        self.assertEqual(new["controller"]["escalation_metrics"], old["controller"]["escalation_metrics"])
        self.assertEqual(new["controller"]["current_event"]["event_id"], old["controller"]["current_event"]["event_id"])
        self.assertEqual(new["dsh"]["session_id"], "original-session")
        self.assertEqual(new["controller"]["current_event"]["lifecycle"], "RESOLVED")
        self.assertEqual(ctl.check(root)["status"], "OK")

    def test_rejects_bad_evidence_wrong_event_and_policy_patch(self):
        for change in ("hash", "event", "policy", "session", "accept"):
            with self.subTest(change=change):
                root, run, request, old = self.setup_case()
                if change == "hash": request["maintenance_resolution"]["evidence"][0]["sha256"] = "bad"
                if change == "event": request["maintenance_resolution"]["event_id"] = "wrong"
                if change == "policy": request["patches"]["runtime"]["defaults"] = {}
                if change == "session": request["patches"]["job"]["dsh"]["session_id"] = "replacement"
                if change == "accept": request["patches"]["job"]["status"] = "ACCEPTED"
                before = ctl.document_hashes(ctl.collect_documents(root))
                with self.assertRaises(ctl.ControlError): self.commit(root, run, request)
                self.assertEqual(before, ctl.document_hashes(ctl.collect_documents(root)))

    def test_sol_cannot_use_user_maintenance(self):
        root, run, request, old = self.setup_case("SOL")
        with self.assertRaisesRegex(ctl.ControlError, "REQUIRES_USER"):
            self.commit(root, run, request)

    def test_sol_can_terminate_at_budget_but_cannot_requeue(self):
        for terminal in (False, True):
            with self.subTest(terminal=terminal):
                root, run, request, old = self.setup_case("SOL")
                del request["maintenance_resolution"]
                if terminal:
                    request["patches"]["job"] = {"status": "FAILED", "decision": {"needs_user": False, "route": "TERMINAL_TECHNICAL_FAILURE"}}
                    self.commit(root, run, request)
                    new = ctl.read_json(root / "jobs/job-one/state.json")
                    self.assertEqual(new["controller"]["current_event"]["lifecycle"], "RESOLVED")
                    self.assertEqual(new["controller"]["escalation_metrics"], old["controller"]["escalation_metrics"])
                else:
                    with self.assertRaisesRegex(ctl.ControlError, "budget would be exceeded"):
                        self.commit(root, run, request)
