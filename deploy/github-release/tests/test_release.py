import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DIRECTORY))
import agent
import bootstrap
import release

SHA = 'a' * 40
RELEASE = SHA + '-123-1'


class Archives(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        self.addCleanup(self.temporary.cleanup)

    def archive(self, members):
        path = self.root / 'source.tar.gz'
        with tarfile.open(path, 'w:gz') as output:
            for name, kind in members:
                item = tarfile.TarInfo(name)
                item.type = kind
                item.size = 3 if kind == tarfile.REGTYPE else 0
                item.linkname = '../../outside'
                output.addfile(item, io.BytesIO(b'abc') if item.size else None)
        return path

    def test_traversal_links_devices_and_duplicates_rejected_before_extracting(self):
        for members in [[('../outside', tarfile.REGTYPE)], [('/outside', tarfile.REGTYPE)],
                        [('link', tarfile.SYMTYPE)], [('hard', tarfile.LNKTYPE)], [('device', tarfile.CHRTYPE)],
                        [('same', tarfile.REGTYPE), ('same', tarfile.REGTYPE)]]:
            with self.subTest(members=members), self.assertRaises(ValueError):
                agent.safe_extract(self.archive(members), self.root / 'output')
        self.assertFalse((self.root / 'output').exists())

    def test_existing_destination_symlink_cannot_escape(self):
        output = self.root / 'output'
        output.mkdir()
        (output / 'link').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            agent.safe_extract(self.archive([('link/escaped', tarfile.REGTYPE)]), output)
        self.assertFalse((self.root / 'escaped').exists())

    def test_expansion_limit(self):
        with self.assertRaises(ValueError):
            agent.safe_extract(self.archive([('file', tarfile.REGTYPE)]), self.root/'output', limit=2)

    def test_regular_source_is_extracted(self):
        agent.safe_extract(self.archive([('billing/file.py', tarfile.REGTYPE)]), self.root/'output')
        self.assertEqual((self.root/'output/billing/file.py').read_bytes(), b'abc')


class HostRollback(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        base = Path(self.temporary.name).resolve()
        self.root = base/'runtime'
        (self.root/'billing').mkdir(parents=True)
        (self.root/'.deployment').mkdir()
        (self.root/'billing/old.py').write_text('previous version')
        (self.root/'.env').write_text('SECRET_KEY=preserved-test-value\nBILLING_APP_IMAGE=previous\n')
        self.config = {'runtime': 'web', 'root': str(self.root), 'releases': str(base/'releases')}
        self.agent = agent.Agent(self.config, RELEASE, 'version-1', 'b'*64)
        (self.agent.stage/'source/billing').mkdir(parents=True)
        (self.agent.stage/'source/billing/new.py').write_text('new version')
        state = self.agent.state()
        state['image_id'] = 'sha256:' + 'c'*64
        self.agent.save(state, 'staged')

    def test_failed_health_restores_deleted_files_image_and_removes_new_files(self):
        with patch.object(agent, 'run', return_value=''), patch.object(agent.time, 'sleep'), \
             patch.object(self.agent, 'health', side_effect=[ValueError('unhealthy')]*20 + [None]):
            with self.assertRaisesRegex(ValueError, 'unhealthy'):
                self.agent.activate()
        self.assertEqual((self.root/'billing/old.py').read_text(), 'previous version')
        self.assertFalse((self.root/'billing/new.py').exists())
        self.assertIn('BILLING_APP_IMAGE=previous', (self.root/'.env').read_text())
        self.assertIn('SECRET_KEY=preserved-test-value', (self.root/'.env').read_text())
        self.assertEqual(self.agent.state()['phase'], 'rolled_back')
        self.assertFalse(self.agent.current.exists())

    def test_collector_failure_restores_original_virtualenv(self):
        self.agent.config['runtime'] = 'collector'
        old = self.root/'.venv'
        old.mkdir()
        (old/'original-dependencies').write_text('preserved')
        (self.agent.stage/'venv').mkdir()
        with patch.object(agent, 'run', return_value=''), patch.object(agent.time, 'sleep'), \
             patch.object(self.agent, 'health', side_effect=[ValueError('unhealthy')]*20 + [None]):
            with self.assertRaisesRegex(ValueError, 'unhealthy'):
                self.agent.activate()
        self.assertFalse(old.is_symlink())
        self.assertEqual((old/'original-dependencies').read_text(), 'preserved')
        self.assertEqual(self.agent.state()['phase'], 'rolled_back')

    def test_unhealthy_rollback_cannot_be_reported_as_success_on_retry(self):
        state = self.agent.state()
        self.agent.save(state, 'rolled_back')
        with patch.object(self.agent, 'health', side_effect=ValueError('previous runtime unhealthy')):
            with self.assertRaisesRegex(ValueError, 'previous runtime unhealthy'):
                self.agent.rollback()

    def test_success_removes_stale_code_and_pins_verified_image(self):
        with patch.object(agent, 'run', return_value=''), patch.object(self.agent, 'health'):
            self.agent.activate()
        self.assertFalse((self.root/'billing/old.py').exists())
        self.assertEqual((self.root/'billing/new.py').read_text(), 'new version')
        self.assertIn('BILLING_APP_IMAGE=sha256:' + 'c'*64, (self.root/'.env').read_text())
        self.assertEqual(json.loads(self.agent.current.read_text())['sha'], SHA)

    def test_rollback_never_overwrites_newer_release(self):
        state = self.agent.state()
        self.agent.save(state, 'active')
        agent.write_json(self.agent.current, {'release_id': 'newer-release'})
        with self.assertRaisesRegex(ValueError, 'different/newer'):
            self.agent.rollback()
        self.assertEqual((self.root/'billing/old.py').read_text(), 'previous version')

    def test_release_manifest_cannot_change_on_retry(self):
        other = agent.Agent(self.config, RELEASE, 'version-2', 'd'*64)
        with self.assertRaisesRegex(ValueError, 'coordinates changed'):
            other.state()

    def test_download_digest_mismatch_blocks_staging(self):
        self.agent.config.update(aws_cli='/usr/bin/aws', region='ap-south-1', bucket='bucket', account_id=release.ACCOUNT)
        (self.agent.stage/'source.tar.gz').write_bytes(b'corrupt')
        with patch.object(agent, 'run', return_value=''), self.assertRaisesRegex(ValueError, 'checksum'):
            self.agent.download('source.tar.gz', 'version', 'd'*64)

    def test_unmatched_installed_executor_blocks_release_before_loading_artifacts(self):
        self.agent.config.update(repository_id=release.REPOSITORY_ID, account_id=release.ACCOUNT, instance_id=release.INSTANCES['web'])
        state = self.agent.state()
        self.agent.save(state, 'new')
        manifest = self.agent.stage/'manifest.json'
        manifest.write_text(json.dumps({'release_id': RELEASE, 'sha': SHA, 'repository_id': release.REPOSITORY_ID,
                            'account_id': release.ACCOUNT, 'instances': release.INSTANCES, 'executor_sha256': 'e'*64}))
        with patch.object(self.agent, 'download', return_value=manifest) as download:
            with self.assertRaisesRegex(ValueError, 'executor changed'):
                self.agent.stage_release()
        self.assertEqual(download.call_count, 1)

    def test_migration_change_blocks_automatic_release(self):
        source = self.agent.stage/'source'
        for name in ['billing/migrations/__init__.py', 'deploy/database-roles.sql', 'deploy/user-administration.sql', 'compose.yaml', 'deploy/collector.service']:
            path = source/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('baseline')
        expected = {str(p.relative_to(source)): agent.sha256(p) for p in source.rglob('*') if p.is_file() and p.name != 'new.py'}
        agent.compatible(source, expected)
        (source/'billing/migrations/0002_change.py').write_text('new schema')
        with self.assertRaisesRegex(ValueError, 'maintenance'):
            agent.compatible(source, expected)


class Coordination(unittest.TestCase):
    def test_second_host_failure_rolls_back_both_hosts_in_reverse_order(self):
        coordinator = release.Release(None, RELEASE, 'version', 'b'*64, {})
        calls = []
        def command(runtime, action):
            calls.append((runtime, action))
            if (runtime, action) == ('web', 'activate'):
                raise RuntimeError('failed')
        with patch.object(coordinator, 'command', side_effect=command), self.assertRaises(RuntimeError):
            coordinator.deploy()
        self.assertEqual(calls[-2:], [('web', 'rollback'), ('collector', 'rollback')])
        self.assertEqual(coordinator.receipt['status'], 'rolled_back')

    def test_stage_failure_does_not_activate_anything(self):
        coordinator = release.Release(None, RELEASE, 'version', 'b'*64, {})
        with patch.object(coordinator, 'command', side_effect=[{}, RuntimeError('bad artifact')]) as command:
            with self.assertRaises(RuntimeError):
                coordinator.deploy()
        self.assertTrue(all(call.args[1] == 'stage' for call in command.call_args_list))

    def test_rollback_failure_is_reported(self):
        coordinator = release.Release(None, RELEASE, 'version', 'b'*64, {})
        with patch.object(coordinator, 'command', side_effect=[{}, {}, {}, RuntimeError('web failure'), RuntimeError('rollback failure'), {}]):
            with self.assertRaises(RuntimeError):
                coordinator.deploy()
        self.assertEqual(coordinator.receipt['status'], 'rollback_failed')
        self.assertEqual(len(coordinator.receipt['rollback_errors']), 1)

    def test_wrong_sha_branch_repository_and_non_manual_run_rejected(self):
        valid = {'GITHUB_SHA': SHA, 'EXPECTED_SHA': SHA, 'GITHUB_REF': release.BRANCH,
                 'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_REPOSITORY_ID': release.REPOSITORY_ID}
        self.assertEqual(release.check_context(valid), SHA)
        for key, value in [('EXPECTED_SHA', 'b'*40), ('GITHUB_REF', 'refs/heads/untrusted'),
                           ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REPOSITORY_ID', 'other')]:
            with self.subTest(key=key), self.assertRaises(ValueError):
                release.check_context(dict(valid, **{key: value}))


class Permissions(unittest.TestCase):
    def test_deployer_has_no_arbitrary_shell_iam_customer_or_secret_access(self):
        trust, policy, reader = bootstrap.policies()
        self.assertEqual(trust['Statement'][0]['Condition']['StringEquals']['token.actions.githubusercontent.com:sub'], bootstrap.SUBJECT)
        permissions = {s['Action'] for s in policy['Statement']}
        self.assertEqual(permissions, {'s3:PutObject', 'ssm:SendCommand', 'ssm:GetCommandInvocation'})
        ssm = next(s for s in policy['Statement'] if s['Action'] == 'ssm:SendCommand')
        self.assertEqual(len(ssm['Resource']), 3)
        self.assertNotIn('AWS-RunShellScript', json.dumps(policy))
        self.assertEqual(reader['Statement'][0]['Action'], 's3:GetObjectVersion')
        self.assertTrue(reader['Statement'][0]['Resource'].endswith('/releases/github/*'))

    def test_document_passes_validated_env_variables_to_fixed_executable(self):
        doc = bootstrap.document()
        for param in doc['parameters'].values():
            self.assertEqual(param['interpolationType'], 'ENV_VAR')
            self.assertTrue('allowedPattern' in param or 'allowedValues' in param)
        shell = '\n'.join(doc['mainSteps'][0]['inputs']['runCommand'])
        self.assertNotIn('{{', shell)
        self.assertIn('/usr/local/lib/cloud-billing-release/agent.py', shell)

    def test_unreviewed_oidc_subject_is_rejected(self):
        with self.assertRaises(ValueError):
            bootstrap.policies('repo:other/repository:environment:billing-production')


if __name__ == '__main__':
    unittest.main()
