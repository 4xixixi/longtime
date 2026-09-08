from __future__ import annotations

import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from control import supervisor_ctl as ctl
from control import exception_handler as handler
from control import handler_capabilities as cap
from tests.test_supervisor_ctl import make_root, write_json
from tests.test_workflow_v81 import rewrite_committed_head


FULL = {"sandbox": {"type": "dangerFullAccess"}, "approvalPolicy": "never"}
PROBE = {"filesystem_read_write": True, "shell": True, "scratch_removed": True, "wsl": None}


def launch_for_test(server, root, envelope, directory):
    # Existing capability tests isolate the platform gate; barrier tests below
    # exercise real controller transactions without bypassing activation_ready.
    write_json(directory / f"{envelope['intent_id']}.request.json", envelope)
    receipt = handler.prepare_thread(server, root, envelope, directory)
    with patch.object(handler, "activation_ready", return_value=True):
        return handler.start_prepared_turn(server, root, envelope, directory, receipt)


class FakeServer:
    def __init__(self, full=True, fail_turn=False):
        self.calls = []
        self.full = full
        self.fail_turn = fail_turn

    def request(self, method, params):
        self.calls.append((method, params))
        if method == "project/list":
            return {"data": [{"id": "project", "roots": [{"path": str(self.root)}]}], "nextCursor": None}
        if method in {"thread/start", "thread/resume"}:
            return {**copy.deepcopy(FULL), "cwd": str(self.root), "model": "user-default-model",
                    "sandbox": {"type": "dangerFullAccess" if self.full else "workspaceWrite"},
                    "thread": {"id": params.get("threadId", "new-thread")}}
        if method == "turn/start":
            if self.fail_turn:
                raise TimeoutError("uncertain turn")
            return {"turn": {"id": "turn", "status": "inProgress"}}
        return {}


class HandlerDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {"event_id": "job:1:FAILED", "intent_id": "intent", "correlation_id": "longtime:intent"}
        self.directory = handler.event_directory(self.root, self.env["event_id"])

    def server(self, **kwargs):
        result = FakeServer(**kwargs)
        result.root = self.root
        return result

    def test_explicit_full_access_on_creation_and_turn_without_model_pin(self):
        server = self.server()
        receipt = launch_for_test(server, self.root, self.env, self.directory)
        for method, params in server.calls:
            if method in {"thread/start", "turn/start"}:
                self.assertEqual(params["permissions"], ":danger-full-access")
                self.assertEqual(params["approvalPolicy"], "never")
                self.assertNotIn("model", params)
                self.assertNotIn("effort", params)
        self.assertEqual(receipt["platform"]["model"], "user-default-model")
        self.assertEqual(next(p for m, p in server.calls if m == "thread/start")["projectId"], "project")

    def test_resume_uses_original_thread_and_full_access(self):
        server = self.server()
        launch_for_test(server, self.root, {**self.env, "thread_id": "original"}, self.directory)
        self.assertEqual(server.calls[0][0], "thread/resume")
        self.assertEqual(server.calls[0][1]["threadId"], "original")
        self.assertNotIn("thread/start", [m for m, _ in server.calls])

    def test_mismatch_preserves_thread_without_starting_turn(self):
        server = self.server(full=False)
        with self.assertRaisesRegex(RuntimeError, "CAPABILITY_MISMATCH"):
            launch_for_test(server, self.root, self.env, self.directory)
        self.assertEqual(ctl.read_json(self.directory / "intent.created.json")["thread_id"], "new-thread")
        self.assertFalse((self.directory / "intent.started.json").exists())
        self.assertNotIn("turn/start", [m for m, _ in server.calls])

    def test_uncertain_turn_preserves_created_identity(self):
        with self.assertRaises(TimeoutError):
            launch_for_test(self.server(fail_turn=True), self.root, self.env, self.directory)
        result = handler.receipt_result(self.root, self.directory / "intent")
        self.assertTrue(result["ok"])  # The allocated identity is recoverable; activation is fenced.
        self.assertTrue((self.directory / "intent.activation-claimed.json").exists())
        self.assertEqual(result["thread_id"], "new-thread")

    def test_receipt_cannot_be_reused_for_other_event_or_thread(self):
        launch_for_test(self.server(), self.root, self.env, self.directory)
        ref = handler.receipt_result(self.root, self.directory / "intent")["dispatch_receipt"]
        for event, thread in (("other", "new-thread"), (self.env["event_id"], "other")):
            with self.assertRaises(ctl.ControlError):
                handler.validate_dispatch_receipt(self.root, ref, event, thread)

    def test_replayed_worker_never_creates_another_task(self):
        self.directory.mkdir(parents=True)
        envelope = self.directory / "intent.request.json"
        write_json(envelope, self.env)
        write_json(self.directory / "intent.worker-claimed.json", {"pid": 123})
        with patch.object(handler, "AppServer") as server:
            handler.worker(self.root, envelope)
            server.assert_not_called()

    def test_observation_recovery_never_hides_uncertain_resume(self):
        self.directory.mkdir(parents=True)
        prefix=self.directory/'intent'
        write_json(prefix.with_suffix('.observed.json'),{'thread_id':'original','status':'idle'})
        self.assertTrue(handler.receipt_result(self.root,prefix)['observation_only'])
        write_json(prefix.with_suffix('.request.json'),self.env)
        result=handler.receipt_result(self.root,prefix)
        self.assertFalse(result['ok'])
        self.assertNotIn('observation_only',result)

    def test_single_worker_lock(self):
        with handler.event_lock(self.directory / "worker.lock"):
            self.assertTrue(handler.lock_held(self.directory))
        self.assertFalse(handler.lock_held(self.directory))


class HandlerCapabilityTests(unittest.TestCase):
    def root(self):
        root = make_root(job_status="FAILED")
        self.addCleanup(__import__('shutil').rmtree, root)
        (root / "workspace").mkdir()
        docs = ctl.collect_documents(root)
        docs["control/runtime.json"]["controller"]["handler_full_access_required"] = True
        event = docs["jobs/job-one/state.json"]["controller"]["current_event"]
        event.update(lifecycle="DISPATCHED", thread_id="handler")
        rewrite_committed_head(root, docs)
        return root, event["event_id"]

    def request(self, root, run, **kwargs):
        req = {"reason": "test", "expected": run["expected"], "patches": {}, "finish": {"outcome": "TEST"}, **kwargs}
        p = root / "request.json"
        write_json(p, req)
        return ctl.commit_request(root, run["lease_token"], p)

    def test_ack_requires_real_preflight(self):
        root, event = self.root()
        run = ctl.acquire_lease(root, "SOL", "handler", event)
        with self.assertRaisesRegex(ctl.ControlError, "PREFLIGHT_REQUIRED"):
            self.request(root, run, event_update={"lifecycle": "ACKNOWLEDGED"}, retain_lease=True)

    def test_restricted_permissions_cannot_ack_or_change_job(self):
        root, event = self.root()
        run = ctl.acquire_lease(root, "SOL", "handler", event)
        lease = ctl.lease_is_current(root, run["lease_token"])
        with patch.object(cap, "platform_snapshot", return_value={"cwd": str(root), "approval_policy": "on-request", "sandbox_policy": {"type": "workspace-write"}}), patch.object(cap, "probe_workspace") as probe:
            result = cap.preflight(root, lease)
            self.assertFalse(result["ok"])
            probe.assert_not_called()
        for extra in ({"event_update": {"lifecycle": "ACKNOWLEDGED"}, "retain_lease": True}, {"patches": {"job": {"status": "QUEUED"}}}):
            with self.assertRaises(ctl.ControlError):
                self.request(root, run, **extra)
        before = ctl.read_json(root / "jobs/job-one/state.json")
        result = self.request(root, run, finish={"outcome": "DISPATCH_CAPABILITY_MISMATCH"})
        self.assertEqual(result["report"]["visibility"], "ATTENTION")
        self.assertEqual(before, ctl.read_json(root / "jobs/job-one/state.json"))

    def test_full_access_ack_preserves_identity_and_budget(self):
        root, event = self.root()
        run = ctl.acquire_lease(root, "SOL", "handler", event)
        lease = ctl.lease_is_current(root, run["lease_token"])
        before = ctl.read_json(root / "jobs/job-one/state.json")
        with patch.object(cap, "platform_snapshot", return_value={**FULL, "cwd": str(root)}), patch.object(cap, "probe_workspace", return_value=PROBE):
            self.assertTrue(cap.preflight(root, lease)["ok"])
            result = self.request(root, run, event_update={"lifecycle": "ACKNOWLEDGED"}, retain_lease=True)
        after = ctl.read_json(root / "jobs/job-one/state.json")
        self.assertEqual(before["controller"]["escalation_metrics"], after["controller"]["escalation_metrics"])
        self.assertEqual(before["controller"]["semantic_revision"], after["controller"]["semantic_revision"])
        self.assertEqual(after["controller"]["current_event"]["event_id"], event)
        self.assertTrue(after["controller"]["current_event"]["capability_receipt"])

    def test_permissions_changed_after_probe_rejected(self):
        root, event = self.root()
        run = ctl.acquire_lease(root, "SOL", "handler", event)
        lease = ctl.lease_is_current(root, run["lease_token"])
        with patch.object(cap, "platform_snapshot", return_value={**FULL, "cwd": str(root)}), patch.object(cap, "probe_workspace", return_value=PROBE):
            cap.preflight(root, lease)
        with patch.object(cap, "platform_snapshot", return_value={"sandbox": {"type": "workspaceWrite"}, "approvalPolicy": "never"}):
            with self.assertRaisesRegex(ctl.ControlError, "CAPABILITY_MISMATCH"):
                self.request(root, run, event_update={"lifecycle": "ACKNOWLEDGED"}, retain_lease=True)

    def test_queue_empty_ack_is_nonsemantic(self):
        root = make_root(active=False, queued=False)
        self.addCleanup(__import__('shutil').rmtree, root)
        docs = ctl.collect_documents(root)
        docs['control/runtime.json']['controller']['handler_full_access_required'] = True
        event = f"project:{docs['control/queue.json']['queue_revision']}:{docs['control/queue.json']['planning_generation']}:QUEUE_EMPTY"
        docs['control/queue.json']['last_empty_event'] = {'event_id':event,'status':'DISPATCHED','thread_id':'handler'}
        rewrite_committed_head(root, docs)
        run = ctl.acquire_lease(root, 'SOL', 'handler', event)
        before = ctl.read_json(root/'control/queue.json')
        lease = ctl.lease_is_current(root,run['lease_token'])
        with patch.object(cap, 'platform_snapshot', return_value={**FULL,'cwd':str(root)}), patch.object(cap, 'probe_workspace', return_value=PROBE):
            cap.preflight(root,lease)
            result = self.request(root,run,queue_event_update={'lifecycle':'ACKNOWLEDGED'},retain_lease=True)
        after = ctl.read_json(root/'control/queue.json')
        self.assertEqual(after['last_empty_event']['status'],'DISPATCHED')
        self.assertTrue(after['last_empty_event']['acknowledged_at'])
        self.assertEqual(result['report']['visibility'],'PROGRESS')
        self.assertEqual(after['queue_revision'],before['queue_revision'])
        self.assertEqual(after['planning_generation'],before['planning_generation'])

    def test_missing_dispatch_receipt_is_rejected(self):
        root = make_root(job_status='FAILED')
        self.addCleanup(__import__('shutil').rmtree,root)
        docs = ctl.collect_documents(root)
        docs['control/runtime.json']['controller']['handler_full_access_required']=True
        rewrite_committed_head(root,docs)
        run=ctl.acquire_lease(root,'LUNA','luna',None)
        intent=ctl.prepare_intent(root,run['lease_token'])['intent']
        with self.assertRaisesRegex(ctl.ControlError,'DISPATCH_RECEIPT_REQUIRED'):
            self.request(root,run,event_update={'lifecycle':'DISPATCHED','thread_id':'new'},resolve_intent={'intent_id':intent['intent_id'],'external_id':'new'})

    def test_valid_dispatch_binds_receipt_without_charging_budget(self):
        root = make_root(job_status='FAILED')
        self.addCleanup(__import__('shutil').rmtree,root)
        docs=ctl.collect_documents(root)
        docs['control/runtime.json']['controller']['handler_full_access_required']=True
        rewrite_committed_head(root,docs)
        run=ctl.acquire_lease(root,'LUNA','luna',None)
        intent=ctl.prepare_intent(root,run['lease_token'])['intent']
        event=run['planned_action']['event_id']
        env={'event_id':event,'intent_id':intent['intent_id'],'correlation_id':intent['correlation_id']}
        server=FakeServer();server.root=root
        directory=handler.event_directory(root,event)
        launch_for_test(server,root,env,directory)
        result=handler.receipt_result(root,directory/intent['intent_id'])
        self.request(root,run,event_update={'lifecycle':'DISPATCHED','thread_id':'new-thread'},
                     dispatch_receipt=result['dispatch_receipt'],resolve_intent={'intent_id':intent['intent_id'],'external_id':'new-thread','summary':'verified'})
        job=ctl.read_json(root/'jobs/job-one/state.json')
        self.assertEqual(job['controller']['current_event']['thread_id'],'new-thread')
        self.assertEqual(job['controller']['semantic_revision'],docs['jobs/job-one/state.json']['controller']['semantic_revision'])
        self.assertEqual(job['controller']['escalation_metrics'],docs['jobs/job-one/state.json']['controller']['escalation_metrics'])

    def test_queue_dispatch_can_recover_original_receipt(self):
        root=make_root(active=False,queued=False)
        self.addCleanup(__import__('shutil').rmtree,root)
        docs=ctl.collect_documents(root)
        docs['control/runtime.json']['controller']['handler_full_access_required']=True
        rewrite_committed_head(root,docs)
        run=ctl.acquire_lease(root,'LUNA','luna',None)
        self.assertEqual(run['planned_action']['type'],'SOL_QUEUE_EMPTY')
        intent=ctl.prepare_intent(root,run['lease_token'])['intent']
        event=run['planned_action']['event_id']
        server=FakeServer();server.root=root
        directory=handler.event_directory(root,event)
        launch_for_test(server,root,{'event_id':event,'intent_id':intent['intent_id'],'correlation_id':intent['correlation_id']},directory)
        self.request(root,run,finish={'outcome':'NO_ACTION'})
        run=ctl.acquire_lease(root,'LUNA','luna',None)
        self.assertEqual(run['planned_action']['type'],'RECOVER_INTENT')
        result=handler.status(root,run['lease_token'])
        self.request(root,run,queue_event_update={'lifecycle':'DISPATCHED','thread_id':'new-thread'},
                     dispatch_receipt=result['dispatch_receipt'],resolve_intent={'intent_id':intent['intent_id'],'external_id':'new-thread','summary':'recovered original'})
        after=ctl.read_json(root/'control/queue.json')
        self.assertEqual(after['last_empty_event']['thread_id'],'new-thread')
        self.assertEqual(after['queue_revision'],docs['control/queue.json']['queue_revision'])

    def test_full_probe_does_not_allow_skipping_ack(self):
        root,event=self.root()
        run=ctl.acquire_lease(root,'SOL','handler',event)
        with patch.object(cap,'platform_snapshot',return_value={**FULL,'cwd':str(root)}),patch.object(cap,'probe_workspace',return_value=PROBE):
            cap.preflight(root,ctl.lease_is_current(root,run['lease_token']))
            with self.assertRaisesRegex(ctl.ControlError,'ACKNOWLEDGEMENT_REQUIRED'):
                self.request(root,run,patches={'job':{'status':'QUEUED'}})

    def test_user_policy_change_is_visible_progress(self):
        root=make_root()
        self.addCleanup(__import__('shutil').rmtree,root)
        run=ctl.acquire_lease(root,'USER',None,None)
        result=self.request(root,run,patches={'runtime':{'controller':{'handler_full_access_required':True}}},finish={'outcome':'HANDLER_FULL_ACCESS_CONFIGURED'})
        self.assertEqual(result['report']['visibility'],'PROGRESS')

    def test_disabled_database_projection_is_not_turn_proof(self):
        self.assertFalse(cap.is_full_access({"sandbox": {"type": "disabled"}, "approvalPolicy": "never"}))

    @unittest.skipUnless(__import__('os').name == 'nt', 'Windows extended path regression')
    def test_unarchived_extended_rollout_path_still_requires_actual_turn_evidence(self):
        import sqlite3
        import json
        from contextlib import closing
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            path = home/'sessions'/'rollout.jsonl'
            path.parent.mkdir()
            path.write_text(json.dumps({'type':'turn_context','payload':{
                'sandbox_policy':{'type':'danger-full-access'},'approval_policy':'never','turn_id':'actual'}})+'\n', encoding='utf-8')
            with closing(sqlite3.connect(home/'state_5.sqlite')) as db:
                db.execute('create table threads(id text,rollout_path text)')
                db.execute('insert into threads values(?,?)', ('handler', '\\\\?\\'+str(path.resolve())))
                db.execute('insert into threads values(?,?)', ('outside', '\\\\?\\'+str((home/'outside.jsonl').resolve())))
                db.commit()
            with patch.dict(__import__('os').environ, {'CODEX_HOME':str(home)}):
                snapshot = cap.platform_snapshot('handler')
                self.assertTrue(cap.is_full_access(snapshot))
                self.assertEqual(snapshot['turn_id'], 'actual')
                with self.assertRaisesRegex(RuntimeError,'ROLLOUT_PATH_INVALID'):
                    cap.platform_snapshot('outside')

    def test_workspace_probe_cleans_only_its_scratch(self):
        with tempfile.TemporaryDirectory() as directory:
            sentinel = Path(directory) / "keep.txt"
            sentinel.write_text("keep")
            self.assertTrue(cap.probe_workspace(directory)["scratch_removed"])
            self.assertEqual(list(Path(directory).iterdir()), [sentinel])


if __name__ == "__main__":
    unittest.main()
