from pathlib import Path
import tempfile
import unittest
from scripts.init_workspace import initialize
from control import supervisor_ctl as ctl
from scripts.set_project_status import set_status


class InitWorkspaceTests(unittest.TestCase):
    def test_new_workspace_is_paused_and_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            spec = base / 'spec.md'
            spec.write_text('# Goal\nTest the example.\n', encoding='utf8')
            root = base / 'controller'
            result = initialize(root, base, spec)
            self.assertTrue(result['ok'])
            self.assertEqual(result['project_status'], 'PAUSED')
            self.assertEqual(result['next_action']['type'], 'NO_ACTION')
            self.assertTrue((root / 'control/exception_handler.py').exists())
            before = (root / 'control/runtime.json').read_bytes()
            with self.assertRaises(FileExistsError):
                initialize(root, base, spec)
            self.assertEqual(before, (root / 'control/runtime.json').read_bytes())
            self.assertTrue(set_status(root, 'ACTIVE', 'Approve fixture execution')['ok'])
            self.assertEqual(ctl.check(root)['next_action']['type'], 'DSH_START')
            self.assertTrue(set_status(root, 'PAUSED', 'Pause fixture')['ok'])
            self.assertEqual(ctl.check(root)['project_status'], 'PAUSED')

    def test_invalid_inputs_do_not_create_root(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            spec = base / 'spec.md'
            spec.write_text('test', encoding='utf8')
            for workspace, job_id in [(base / 'missing', 'one'), (base, '../escape')]:
                with self.assertRaises(ValueError):
                    initialize(base / 'new', workspace, spec, job_id)
                self.assertFalse((base / 'new').exists())
