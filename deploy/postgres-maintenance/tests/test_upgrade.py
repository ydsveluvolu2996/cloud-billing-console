import argparse
import copy
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


class CatalogSecurityComparison(unittest.TestCase):
    def snapshot(self):
        return {'extensions': [{'extname': 'btree_gist', 'extversion': '1.7', 'owner': 'billing_admin', 'namespace': 'public'}],
                'functions': [{'signature': 'public.existing(integer)', 'extension': 'btree_gist', 'owner': 'billing_admin', 'prosecdef': False,
                               'proconfig': None, 'proacl': ['=X/billing_admin', 'billing_admin=X/billing_admin'],
                               'default_acl': ['=X/billing_admin', 'billing_admin=X/billing_admin']}],
                'roles': [{'rolname': 'billing_web', 'rolbypassrls': False}], 'relations': []}

    def changed(self):
        source = self.snapshot()
        target = copy.deepcopy(source)
        target['extensions'][0]['extversion'] = '1.8'
        added = copy.deepcopy(target['functions'][0])
        added['signature'] = 'public.new_sortsupport(internal)'
        target['functions'].append(added)
        return source, target

    def test_new_default_privilege_function_only_for_existing_upgraded_extension(self):
        source, target = self.changed()
        self.assertTrue(upgrade.catalog_equivalent(source, target))
        target['extensions'][0]['extversion'] = '1.7'
        self.assertFalse(upgrade.catalog_equivalent(source, target))

    def test_reject_removed_or_security_changed_existing_function(self):
        source, target = self.changed()
        target['functions'][0]['owner'] = 'billing_migration_bootstrap'
        self.assertFalse(upgrade.catalog_equivalent(source, target))
        target['functions'].pop(0)
        self.assertFalse(upgrade.catalog_equivalent(source, target))

    def test_reject_new_application_function_or_definer_or_nondefault_grants(self):
        for field, value in [('extension', None), ('prosecdef', True), ('proacl', ['attacker=X/billing_admin']), ('proconfig', ['search_path=unsafe'])]:
            source, target = self.changed()
            target['functions'][-1][field] = value
            self.assertFalse(upgrade.catalog_equivalent(source, target))

    def test_reject_extension_owner_schema_and_role_drift(self):
        for field in ('owner', 'namespace'):
            source, target = self.changed()
            target['extensions'][0][field] = 'unexpected'
            self.assertFalse(upgrade.catalog_equivalent(source, target))
        source, target = self.changed()
        target['roles'][0]['rolbypassrls'] = True
        self.assertFalse(upgrade.catalog_equivalent(source, target))


if __name__ == '__main__':
    unittest.main()

class RestoreGlobals(unittest.TestCase):
    def test_only_existing_admin_create_is_removed(self):
        text = 'CREATE ROLE billing_admin;\nALTER ROLE billing_admin WITH SUPERUSER;\nCREATE ROLE billing_web;\n'
        result = upgrade.restore_globals(text)
        self.assertNotIn('CREATE ROLE billing_admin;', result)
        self.assertIn('ALTER ROLE billing_admin WITH SUPERUSER;', result)
        self.assertIn('CREATE ROLE billing_web;', result)
        for invalid in ('', text + 'CREATE ROLE billing_admin;\n'):
            with self.assertRaises(RuntimeError):
                upgrade.restore_globals(invalid)

    def test_legacy_initializer_restrictions_are_applied_last(self):
        text = 'CREATE ROLE billing;\nALTER ROLE billing WITH NOSUPERUSER NOLOGIN;\nCREATE ROLE billing_admin;\nALTER ROLE billing_admin WITH SUPERUSER LOGIN;\n'
        result = upgrade.restore_globals(text, 'billing')
        self.assertNotIn('CREATE ROLE billing;', result)
        self.assertLess(result.index('ALTER ROLE billing_admin'), result.index('SET ROLE billing_admin;'))
        self.assertGreater(result.index('ALTER ROLE billing WITH'), result.index('SET ROLE billing_admin;'))
        with self.assertRaises(RuntimeError):
            upgrade.restore_globals(text, 'billing; DROP ROLE billing_web')
