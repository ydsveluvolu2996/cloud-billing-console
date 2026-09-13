import argparse
import os
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

SPEC = importlib.util.spec_from_file_location('pg18_upgrade', Path(__file__).parents[1] / 'upgrade.py')
upgrade = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(upgrade)


class UpgradeGuards(unittest.TestCase):
    def setUp(self):
        original_cwd = Path.cwd()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(os.chdir, original_cwd)
        root = Path(self.directory.name)
        (root / '.deployment/secrets').mkdir(parents=True)
        (root / '.deployment/secrets/admin_db_password').write_text('test-private-value')
        self.runner = upgrade.Maintenance(argparse.Namespace(root=str(root), image='sha256:' + 'a' * 64,
            volume='new-volume-pg18', runtime_checks_passed=False))

    def test_env_changes_preserve_other_settings_and_remove_duplicate_keys(self):
        changed = upgrade.env_patch('SECRET_FILE=/run/secret\n# note\nBILLING_POSTGRES_VOLUME=old\nBILLING_POSTGRES_VOLUME=also-old\n', {'BILLING_POSTGRES_VOLUME': 'new'})
        self.assertEqual(changed.count('BILLING_POSTGRES_VOLUME='), 1)
        self.assertIn('SECRET_FILE=/run/secret\n# note\n', changed)
        self.assertTrue(changed.endswith('BILLING_POSTGRES_VOLUME=new\n'))

    def test_password_is_only_in_child_environment_not_argv(self):
        with patch.object(upgrade.subprocess, 'run', return_value=Mock(returncode=0, stdout=b'17', stderr=b'')) as run:
            self.runner.sql('container', 'SHOW server_version')
        argv = run.call_args.args[0]
        self.assertIn('PGPASSWORD', argv)
        self.assertNotIn('test-private-value', ' '.join(argv))
        self.assertEqual(run.call_args.kwargs['env']['PGPASSWORD'], 'test-private-value')

    def test_restore_refuses_existing_volume_without_deleting_it(self):
        self.runner.command = Mock(return_value=b'new-volume-pg18\n')
        with self.assertRaisesRegex(RuntimeError, 'never overwrite'):
            self.runner.restore('new-volume-pg18', 'final')
        self.assertEqual(self.runner.command.call_count, 1)

    def test_cutover_requires_completed_rehearsal(self):
        self.runner.command = Mock()
        with self.assertRaisesRegex(RuntimeError, 'rehearsal'):
            self.runner.cutover()
        self.runner.command.assert_not_called()

    def test_rollback_forbidden_after_writer_boundary(self):
        self.runner.state = {'phase': 'writers-reopened'}
        self.runner.command = Mock()
        with self.assertRaisesRegex(RuntimeError, 'only before'):
            self.runner.rollback()
        self.runner.command.assert_not_called()

    def test_reopen_requires_explicit_runtime_verification(self):
        self.runner.state = {'phase': 'awaiting-verification'}
        self.runner.command = Mock()
        with self.assertRaisesRegex(RuntimeError, 'runtime-checks-passed'):
            self.runner.reopen()
        self.runner.command.assert_not_called()

    def test_sequence_verification_records_called_state_and_last_value(self):
        self.runner.sql = Mock(side_effect=['["billing_cost"]', '42', '["billing_cost_id_seq"]', '99:true'])
        result = self.runner.counts('container')
        self.assertEqual(result, {'billing_cost': 42, 'sequence:billing_cost_id_seq': '99:true'})


if __name__ == '__main__':
    unittest.main()
