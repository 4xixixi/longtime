from __future__ import annotations

import copy
import shutil
import unittest
from unittest import mock

from control import supervisor_ctl as ctl
from tests.test_supervisor_ctl import make_root, write_json


class TransactionRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = make_root()
        self.addCleanup(shutil.rmtree, self.root)

    def pending(self, name="99999999T999999999999Z-pending"):
        directory = self.root / "control" / "transactions" / name
        directory.mkdir()
        documents = ctl.collect_documents(self.root)
        documents["control/runtime.json"]["next_expected_run_at"] = "2030-01-01T00:00:00.000Z"
        return directory, {"transaction_id": name, "documents": documents}

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*") if p.is_file()}

    def assert_rejected_without_writes(self, directory, prepare, pattern):
        before = self.snapshot()
        with self.assertRaisesRegex(ctl.ControlError, pattern):
            ctl.roll_forward(self.root, directory, prepare)
        self.assertEqual(before, self.snapshot())

    def test_invalid_last_document_does_not_write_earlier_documents(self):
        directory, prepare = self.pending()
        prepare["documents"]["zzz-invalid.json"] = []
        self.assert_rejected_without_writes(directory, prepare, "not an object")

    def test_invalid_document_does_not_publish_protocol_files(self):
        directory, prepare = self.pending()
        data = b"new specification"
        prepare["file_updates"] = {"control/staging/test.md": {
            "encoding": "base64", "data": ctl.base64.b64encode(data).decode(),
            "sha256": ctl.digest_bytes(data)}}
        prepare["documents"]["zzz-invalid.json"] = []
        self.assert_rejected_without_writes(directory, prepare, "not an object")

    def test_transaction_id_must_match_directory(self):
        directory, prepare = self.pending()
        prepare["transaction_id"] = "different"
        self.assert_rejected_without_writes(directory, prepare, "transaction_id")

    def test_intent_resolution_cannot_escape_intents_directory(self):
        directory, prepare = self.pending()
        prepare["intent_resolution"] = {"intent_id": "../escaped"}
        self.assert_rejected_without_writes(directory, prepare, "intent_id")

    def test_absolute_document_path_is_rejected_even_inside_root(self):
        directory, prepare = self.pending()
        prepare["documents"][str(self.root / "absolute.json")] = {}
        self.assert_rejected_without_writes(directory, prepare, "relative path")

    def test_document_and_file_update_cannot_share_destination(self):
        directory, prepare = self.pending()
        data = b"conflicting content"
        prepare["file_updates"] = {"control/runtime.json": {
            "encoding": "base64", "data": ctl.base64.b64encode(data).decode(),
            "sha256": ctl.digest_bytes(data)}}
        self.assert_rejected_without_writes(directory, prepare, "duplicate transaction destination")

    def test_document_path_cannot_escape_root(self):
        directory, prepare = self.pending()
        prepare["documents"]["zzz/../../escaped.json"] = {}
        self.assert_rejected_without_writes(directory, prepare, "escapes root")

    def test_real_write_failures_remain_recoverable_at_every_boundary(self):
        # Include protocol publication, state snapshots, intent marker and commit.
        for failure_index in range(7):
            with self.subTest(failure_index=failure_index):
                directory, prepare = self.pending(f"99999999T999999999999Z-crash-{failure_index}")
                data = b"protocol payload"
                prepare["file_updates"] = {"control/staging/recovered.md": {
                    "encoding": "base64", "data": ctl.base64.b64encode(data).decode(),
                    "sha256": ctl.digest_bytes(data)}}
                prepare["intent_resolution"] = {"intent_id": "test-intent", "outcome": "COMPLETED"}
                write_json(directory / "prepare.json", prepare)
                original = ctl.atomic_write_bytes
                calls = 0

                def fail_once(path, payload):
                    nonlocal calls
                    calls += 1
                    if calls == failure_index + 1:
                        raise OSError("simulated storage failure")
                    original(path, payload)

                with mock.patch.object(ctl, "atomic_write_bytes", side_effect=fail_once):
                    with self.assertRaisesRegex(OSError, "simulated storage failure"):
                        ctl.roll_forward(self.root, directory, prepare)
                self.assertEqual(ctl.recover_transactions(self.root), [directory.name])
                self.assertEqual(ctl.recover_transactions(self.root), [])
                self.assertEqual(ctl.document_hashes(ctl.collect_documents(self.root)),
                                 ctl.document_hashes(prepare["documents"]))
                self.assertEqual((self.root / "control/staging/recovered.md").read_bytes(), data)
                self.assertTrue((self.root / "control/intents/test-intent.resolved.json").exists())
                self.assertTrue(ctl.check(self.root)["ok"])

    def test_corrupt_commit_is_not_silently_skipped_by_recovery(self):
        directory = ctl.latest_committed_transaction(self.root)[0]
        write_json(directory / "commit.json", {"transaction_id": directory.name})
        before = self.snapshot()
        with self.assertRaisesRegex(ctl.ControlError, "CORRUPT_COMMITTED_TRANSACTION"):
            ctl.recover_transactions(self.root)
        self.assertEqual(before, self.snapshot())

    def test_pending_transaction_older_than_head_cannot_roll_back_state(self):
        directory, prepare = self.pending("00000000T000000000000Z-old")
        write_json(directory / "prepare.json", prepare)
        before = self.snapshot()
        with self.assertRaisesRegex(ctl.ControlError, "OUT_OF_ORDER_PENDING_TRANSACTION"):
            ctl.recover_transactions(self.root)
        self.assertEqual(before, self.snapshot())

    def test_all_pending_transactions_are_validated_before_any_replay(self):
        first, prepare = self.pending("99999999T999999999999Z-a")
        write_json(first / "prepare.json", prepare)
        second, invalid = self.pending("99999999T999999999999Z-b")
        invalid["documents"]["zzz-invalid.json"] = []
        write_json(second / "prepare.json", invalid)
        before = self.snapshot()
        with self.assertRaises(ctl.ControlError):
            ctl.recover_transactions(self.root)
        self.assertEqual(before, self.snapshot())

    def test_latest_head_reads_newest_valid_pair_first(self):
        head = ctl.latest_committed_transaction(self.root)
        # Directory ordering is the existing store's transaction ordering.
        for index in range(4):
            directory = self.root / "control" / "transactions" / f"00000000-{index}"
            directory.mkdir()
            prepare = copy.deepcopy(head[1])
            commit = copy.deepcopy(head[2])
            prepare["transaction_id"] = commit["transaction_id"] = directory.name
            write_json(directory / "prepare.json", prepare)
            write_json(directory / "commit.json", commit)
        with mock.patch.object(ctl, "_validated_committed_transaction",
                               wraps=ctl._validated_committed_transaction) as validate:
            self.assertEqual(ctl.latest_committed_transaction(self.root)[0], head[0])
        self.assertEqual(validate.call_count, 1)


if __name__ == "__main__":
    unittest.main()
