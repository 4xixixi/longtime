"""Transaction/activation ordering and crash recovery; no real model turns."""
import copy
import shutil
import unittest
from unittest.mock import patch

from control import exception_handler as handler
from control import supervisor_ctl as ctl
from tests.test_handler_full_access import FakeServer
from tests.test_supervisor_ctl import make_root, write_json
from tests.test_workflow_v81 import rewrite_committed_head


class HandoffTests(unittest.TestCase):
    def setup_case(self, queue=False, resume=False):
        self.root = make_root(active=not queue, queued=not queue, job_status="FAILED")
        self.addCleanup(shutil.rmtree, self.root)
        docs = ctl.collect_documents(self.root)
        docs['control/runtime.json']['controller']['handler_full_access_required'] = True
        if resume:
            if queue:
                q = docs['control/queue.json']
                q['last_empty_event'] = {'event_id': f"project:{q['queue_revision']}:{q['planning_generation']}:QUEUE_EMPTY",
                                          'status': 'DISPATCHED', 'thread_id': 'original'}
            else:
                docs['jobs/job-one/state.json']['controller']['current_event'].update(lifecycle='DISPATCHED', thread_id='original')
        rewrite_committed_head(self.root, docs)
        self.before = copy.deepcopy(docs)
        self.run = ctl.acquire_lease(self.root, 'LUNA', 'luna', None)
        intent = ctl.prepare_intent(self.root, self.run['lease_token'])['intent']
        self.env = {'event_id': self.run['planned_action']['event_id'], 'intent_id': intent['intent_id'],
                    'correlation_id': intent['correlation_id'], 'thread_id': self.run['planned_action'].get('thread_id')}
        self.directory = handler.event_directory(self.root, self.env['event_id'])
        self.prefix = self.directory / self.env['intent_id']
        write_json(self.prefix.with_suffix('.request.json'), self.env)
        self.server = FakeServer(); self.server.root = self.root
        self.receipt = handler.prepare_thread(self.server, self.root, self.env, self.directory)
        ref = handler.receipt_result(self.root, self.prefix)['dispatch_receipt']
        update = {'recovery_requested_at': ctl.iso_utc(), 'recovery_outcome': 'PREPARED'} if resume else {
            'lifecycle': 'DISPATCHED', 'thread_id': self.receipt['thread_id']}
        self.request = {'reason': 'handoff test', 'expected': self.run['expected'], 'patches': {},
                        'queue_event_update' if queue else 'event_update': update, 'dispatch_receipt': ref,
                        'resolve_intent': {'intent_id': intent['intent_id'], 'external_id': self.receipt['thread_id'], 'summary': 'prepared'},
                        'finish': {'outcome': 'HANDLER_PREPARED'}}

    def commit(self):
        path = self.root/'request.json'; write_json(path, self.request)
        return ctl.commit_request(self.root, self.run['lease_token'], path)

    def ready(self):
        return handler.activation_ready(self.root, self.env, self.receipt)

    def test_prepare_does_not_start_model_and_commit_release_unlocks_activation(self):
        self.setup_case()
        self.assertNotIn('turn/start', [m for m, _ in self.server.calls])
        self.assertFalse(self.ready())
        with self.assertRaisesRegex(RuntimeError, 'NOT_COMMITTED'):
            handler.start_prepared_turn(self.server, self.root, self.env, self.directory, self.receipt)
        self.commit()
        self.assertTrue(self.ready())
        handler.start_prepared_turn(self.server, self.root, self.env, self.directory, self.receipt)
        self.assertEqual(sum(m == 'turn/start' for m, _ in self.server.calls), 1)
        with self.assertRaises(ctl.ControlError):
            handler.start_prepared_turn(self.server, self.root, self.env, self.directory, self.receipt)
        self.assertEqual(sum(m == 'turn/start' for m, _ in self.server.calls), 1)
        after = ctl.read_json(self.root/'jobs/job-one/state.json')
        for key in ('semantic_revision', 'event_generation', 'escalation_metrics'):
            self.assertEqual(after['controller'][key], self.before['jobs/job-one/state.json']['controller'][key])

    def test_commit_written_but_parent_lease_not_released_cannot_activate(self):
        self.setup_case()
        lease = ctl.read_json(ctl.control_paths(self.root)['lease'])
        self.commit()
        write_json(ctl.control_paths(self.root)['lease'], lease)  # Simulate crash before unlink.
        self.assertFalse(self.ready())
        lease['expires_at'] = '2000-01-01T00:00:00.000Z'
        write_json(ctl.control_paths(self.root)['lease'], lease)
        self.assertFalse(self.ready())  # Expiry alone cannot authorize activation.
        self.assertTrue(ctl.recover(self.root)['ok'])
        self.assertTrue(self.ready())

    def test_intent_resolution_without_completed_transaction_cannot_activate(self):
        self.setup_case()
        def crash(_root, transaction_dir, prepare):
            writes, _, _ = ctl.prepare_transaction_writes(_root, transaction_dir, prepare)
            for path, data in writes:
                ctl.atomic_write_bytes(path, data)
            raise RuntimeError('power loss before commit marker')
        with patch.object(ctl, 'roll_forward', side_effect=crash):
            with self.assertRaisesRegex(RuntimeError, 'power loss'):
                self.commit()
        lease_path = ctl.control_paths(self.root)['lease']
        lease_path.unlink()  # Even an absent lease is insufficient with a pending txn.
        self.assertFalse(self.ready())
        self.assertTrue(ctl.recover(self.root)['ok'])
        self.assertTrue(self.ready())

    def test_prepared_intent_recovers_after_parent_finishes_without_binding(self):
        self.setup_case()
        saved = copy.deepcopy(self.request)
        self.request = {'reason': 'interrupted dispatch', 'expected': self.run['expected'], 'patches': {}, 'finish': {'outcome': 'NO_ACTION'}}
        self.commit()
        self.assertFalse(self.ready())
        self.run = ctl.acquire_lease(self.root, 'LUNA', 'next-luna', None)
        self.assertEqual(self.run['planned_action']['type'], 'RECOVER_INTENT')
        with patch.object(handler, 'ensure_worker') as rearm:
            result = handler.status(self.root, self.run['lease_token'])
            rearm.assert_called_once()
        self.assertEqual(result['thread_id'], self.receipt['thread_id'])
        self.request = {**saved, 'expected': self.run['expected']}
        self.commit()
        self.assertTrue(self.ready())

    def test_resume_receipt_is_bound_in_job_transaction(self):
        self.setup_case(resume=True)
        self.assertEqual(self.receipt['thread_id'], 'original')
        self.assertNotIn('thread/start', [m for m, _ in self.server.calls])
        self.assertEqual(self.commit()['report']['visibility'], 'PROGRESS')
        self.assertTrue(self.ready())

    def test_queue_dispatch_and_resume_use_same_barrier(self):
        self.setup_case(queue=True, resume=True)
        self.assertFalse(self.ready())
        self.assertEqual(self.commit()['report']['visibility'], 'PROGRESS')
        self.assertTrue(self.ready())
        q = ctl.read_json(self.root/'control/queue.json')
        self.assertEqual(q['queue_revision'], self.before['control/queue.json']['queue_revision'])
        self.assertEqual(q['planning_generation'], self.before['control/queue.json']['planning_generation'])

    def test_dead_waiting_worker_reloads_original_thread_without_allocation(self):
        self.setup_case()
        second = FakeServer(); second.root = self.root
        receipt = handler.prepare_thread(second, self.root, self.env, self.directory)
        self.assertEqual(receipt, self.receipt)
        self.assertEqual(second.calls[0][0], 'thread/resume')
        self.assertNotIn('thread/start', [m for m, _ in second.calls])
        self.assertNotIn('turn/start', [m for m, _ in second.calls])

    def test_archived_original_is_unarchived_without_replacement_or_turn(self):
        self.setup_case(resume=True)
        class ArchivedServer(FakeServer):
            archived = True
            def request(self, method, params):
                if method == 'thread/resume' and self.archived:
                    self.calls.append((method, params))
                    raise RuntimeError('session original is archived. Run codex unarchive')
                if method == 'thread/unarchive':
                    self.archived = False
                return super().request(method, params)
        server = ArchivedServer(); server.root = self.root
        result = handler.prepare_thread(server, self.root, self.env, self.directory)
        self.assertEqual(result['thread_id'], 'original')
        self.assertEqual([m for m, _ in server.calls], ['thread/resume', 'thread/unarchive', 'thread/resume'])

    def test_losing_worker_cannot_overwrite_live_owner_status(self):
        self.setup_case()
        write_json(self.prefix.with_suffix('.status.json'), {'status': 'waiting_for_commit'})
        with handler.event_lock(self.directory/'worker.lock'), patch.object(handler, 'AppServer') as server:
            handler.worker(self.root, self.prefix.with_suffix('.request.json'))
            server.assert_not_called()
        self.assertEqual(ctl.read_json(self.prefix.with_suffix('.status.json')), {'status': 'waiting_for_commit'})

    def test_committed_waiting_worker_is_rearmed_by_status_without_new_intent_dispatch(self):
        self.setup_case()
        self.commit()
        run = ctl.acquire_lease(self.root, 'LUNA', 'next', None)
        ctl.prepare_intent(self.root, run['lease_token'])
        with patch.object(handler, 'ensure_worker') as rearm, patch.object(handler, 'AppServer') as server:
            result = handler.status(self.root, run['lease_token'])
            rearm.assert_called_once_with(self.root, self.prefix.with_suffix('.request.json'))
            server.assert_not_called()
        self.assertEqual(result['activation'], 'pending_commit_or_release')

    def test_unknown_activation_is_not_replayed(self):
        self.setup_case()
        self.commit()
        self.server.fail_turn = True
        with self.assertRaises(TimeoutError):
            handler.start_prepared_turn(self.server, self.root, self.env, self.directory, self.receipt)
        with patch.object(handler.subprocess, 'Popen') as popen:
            handler.ensure_worker(self.root, self.prefix.with_suffix('.request.json'))
            popen.assert_not_called()
        run = ctl.acquire_lease(self.root, 'LUNA', 'next', None)
        with self.assertRaisesRegex(ctl.ControlError, 'OUTCOME_UNKNOWN'):
            handler.status(self.root, run['lease_token'])

    def test_slow_parent_over_60_wait_cycles_never_starts_model_before_commit(self):
        self.setup_case()
        owner = self
        class Server(FakeServer):
            notifications = []
            def __enter__(self): return self
            def __exit__(self, *_): pass
            def receive(self, timeout):
                return {'method': 'turn/completed', 'params': {'threadId': owner.receipt['thread_id'], 'turn': {'id': 'turn', 'status': 'completed'}}}
            def request(self, method, params):
                if method == 'turn/start':
                    owner.assertGreaterEqual(len(waits), 90)
                    owner.assertTrue(owner.ready())
                return super().request(method, params)
        server = Server(); server.root = self.root
        waits = []
        def wait(seconds):
            waits.append(seconds)
            self.assertNotIn('turn/start', [m for m, _ in server.calls])
            if len(waits) == 90:
                self.commit()
        with patch.object(handler, 'AppServer', return_value=server), patch.object(handler.time, 'sleep', side_effect=wait):
            handler.worker(self.root, self.prefix.with_suffix('.request.json'))
        self.assertEqual(len(waits), 90)
        self.assertEqual(sum(m == 'turn/start' for m, _ in server.calls), 1)
        self.assertNotIn('thread/start', [m for m, _ in server.calls])
        self.assertEqual(ctl.read_json(self.prefix.with_suffix('.status.json'))['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
