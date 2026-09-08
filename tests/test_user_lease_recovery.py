import tempfile
import unittest
from pathlib import Path
from control import supervisor_ctl as ctl


class UserLeaseRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'control/run-lease.json'
        self.path.parent.mkdir()
        self.lease = {'run_id': 'original', 'token': 'old-token', 'role': 'LUNA',
                      'source_thread_id': None, 'routing_action_deadline_at': '2020-01-01T00:00:00Z'}
        ctl.atomic_write_json(self.path, self.lease)

    def test_preserves_receipt_and_revokes_old_token(self):
        ctl.reclaim_abandoned_luna(self.root, 'original', 'User confirms ended; recover now')
        self.assertFalse(self.path.exists())
        archived = list((self.root / 'control/stale-leases').glob('*.json'))
        self.assertEqual(ctl.read_json(archived[0]), self.lease)
        self.assertEqual(len(list((self.root / 'control/diagnostics/user-lease-recovery').glob('*.json'))), 1)

    def test_refuses_other_run_and_missing_authorization(self):
        for run, auth in [('different', 'authorized'), ('original', '')]:
            with self.assertRaises(ctl.ControlError):
                ctl.reclaim_abandoned_luna(self.root, run, auth)
            self.assertEqual(ctl.read_json(self.path), self.lease)

    def test_refuses_bound_active_and_external_actions(self):
        for change in [{'role': 'SOL'}, {'source_thread_id': 'thread'}, {'intent_id': 'intent'},
                       {'routing_action_deadline_at': '2099-01-01T00:00:00Z'}]:
            ctl.atomic_write_json(self.path, {**self.lease, **change})
            with self.assertRaises(ctl.ControlError):
                ctl.reclaim_abandoned_luna(self.root, 'original', 'authorized')
            self.assertTrue(self.path.exists())


if __name__ == '__main__':
    unittest.main()
