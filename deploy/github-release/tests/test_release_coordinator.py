"""Release authorization, stale-push protection and sanitized host receipts."""
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import yaml

DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DIRECTORY))
import release

SHA = 'a' * 40
NEW_SHA = 'b' * 40
RELEASE_ID = SHA + '-123-1'
SECRET = 'sensitive-environment-value'


def context(event='push', **changes):
    env = {'GITHUB_EVENT_NAME': event, 'GITHUB_REF': release.BRANCH,
           'GITHUB_REPOSITORY': release.REPOSITORY, 'GITHUB_REPOSITORY_ID': release.REPOSITORY_ID,
           'GITHUB_SHA': SHA, 'GITHUB_TOKEN': 'synthetic-github-token', 'EXPECTED_SHA': SHA}
    env.update(changes)
    return env


class Preflight(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        previous = Path.cwd()
        os.chdir(self.directory)
        self.addCleanup(os.chdir, previous)
        self.env = context(GITHUB_OUTPUT=str(self.directory / 'output'),
                           GITHUB_STEP_SUMMARY=str(self.directory / 'summary'))

    def test_current_push_authorized_without_manual_expected_sha(self):
        env = dict(self.env)
        env.pop('EXPECTED_SHA')
        with patch.object(release, 'current_branch_sha', return_value=SHA) as head:
            self.assertTrue(release.preflight(env))
        head.assert_called_once_with(env)
        self.assertEqual((self.directory / 'output').read_text(), 'deploy=true\n')
        self.assertFalse((self.directory / 'release-receipt.json').exists())

    def test_superseded_push_skips_before_any_aws_client_or_artifact_read(self):
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(release, 'current_branch_sha', return_value=NEW_SHA), \
                patch.object(release.boto3, 'Session') as aws:
            release.main('/nonexistent-release-artifact')
        aws.assert_not_called()
        receipt = json.loads((self.directory / 'release-receipt.json').read_text())
        self.assertEqual(receipt, {'sha': SHA, 'status': 'skipped_superseded', 'current_branch_sha': NEW_SHA})
        self.assertEqual((self.directory / 'output').read_text(), 'deploy=false\n')
        self.assertIn('skipped_superseded', (self.directory / 'summary').read_text())

    def test_unverifiable_push_fails_closed_before_aws(self):
        with patch.dict(os.environ, self.env, clear=True), \
                patch.object(release, 'current_branch_sha', side_effect=RuntimeError('verification unavailable')), \
                patch.object(release.boto3, 'Session') as aws:
            with self.assertRaisesRegex(RuntimeError, 'verification unavailable'):
                release.main('/nonexistent-release-artifact')
        aws.assert_not_called()
        self.assertFalse((self.directory / 'output').exists())

    def test_manual_dispatch_keeps_exact_sha_and_does_not_follow_branch_head(self):
        with patch.object(release, 'current_branch_sha') as head:
            self.assertTrue(release.preflight(context('workflow_dispatch')))
            for expected in ('', NEW_SHA):
                with self.subTest(expected=expected), self.assertRaises(ValueError):
                    release.preflight(context('workflow_dispatch', EXPECTED_SHA=expected))
        head.assert_not_called()

    def test_wrong_identity_branch_event_or_sha_stops_before_lookup_and_aws(self):
        invalid = [('GITHUB_REPOSITORY', 'other/cloud-billing-console'),
                   ('GITHUB_REPOSITORY_ID', '1'), ('GITHUB_REF', release.BRANCH + '-other'),
                   ('GITHUB_REF', release.BRANCH.replace('refs/heads/', 'refs/tags/')),
                   ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_EVENT_NAME', 'schedule'),
                   ('GITHUB_SHA', 'a' * 39), ('GITHUB_SHA', 'A' * 40)]
        for key, value in invalid:
            with self.subTest(key=key, value=value), \
                    patch.dict(os.environ, context(**{key: value}), clear=True), \
                    patch.object(release, 'current_branch_sha') as head, \
                    patch.object(release.boto3, 'Session') as aws:
                with self.assertRaises(ValueError):
                    release.main('/nonexistent-release-artifact')
                head.assert_not_called()
                aws.assert_not_called()


class BranchLookup(unittest.TestCase):
    def response(self, payload):
        response = io.StringIO(json.dumps(payload))
        opener = Mock()
        opener.open.return_value = response
        return opener

    def test_uses_exact_fixed_ref_endpoint_and_authenticated_read(self):
        opener = self.response({'ref': release.BRANCH, 'object': {'type': 'commit', 'sha': SHA}})
        with patch.object(release.urllib.request, 'build_opener', return_value=opener) as build:
            self.assertEqual(release.current_branch_sha(context()), SHA)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, 'https://api.github.com/repos/' + release.REPOSITORY +
                         '/git/ref/heads/codex/billing-security-portfolio')
        self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-github-token')
        self.assertEqual(opener.open.call_args.kwargs, {'timeout': 30})
        self.assertIsInstance(build.call_args.args[0], release.NoRedirect)
        self.assertIsNone(build.call_args.args[0].redirect_request(None, None, 302, '', {}, 'https://other.invalid'))

    def test_wrong_ref_type_sha_and_malformed_responses_fail_closed(self):
        payloads = [[], {}, {'ref': release.BRANCH + '-other', 'object': {'type': 'commit', 'sha': SHA}},
                    {'ref': release.BRANCH, 'object': {'type': 'tag', 'sha': SHA}},
                    {'ref': release.BRANCH, 'object': {'type': 'commit', 'sha': 'short'}},
                    {'ref': release.BRANCH, 'object': {'type': 'commit', 'sha': []}}]
        for payload in payloads:
            with self.subTest(payload=payload), \
                    patch.object(release.urllib.request, 'build_opener', return_value=self.response(payload)):
                with self.assertRaisesRegex(RuntimeError, '^Cannot verify the current release branch; deployment stopped$'):
                    release.current_branch_sha(context())

    def test_http_exception_does_not_expose_response_or_token(self):
        opener = Mock()
        opener.open.side_effect = OSError(SECRET)
        with patch.object(release.urllib.request, 'build_opener', return_value=opener), \
                self.assertRaises(RuntimeError) as caught:
            release.current_branch_sha(context())
        self.assertNotIn(SECRET, str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)

    def test_missing_token_does_not_attempt_network(self):
        with patch.object(release.urllib.request, 'build_opener') as build, self.assertRaises(ValueError):
            release.current_branch_sha(context(GITHUB_TOKEN=''))
        build.assert_not_called()


class HelperReceipts(unittest.TestCase):
    def coordinator(self, result):
        ssm = Mock()
        ssm.send_command.return_value = {'Command': {'CommandId': '00000000-0000-0000-0000-000000000001'}}
        ssm.get_command_invocation.return_value = result
        return release.Release(ssm, RELEASE_ID, 'version', 'b' * 64, {})

    def test_known_error_code_is_retained_without_raw_helper_error_or_stderr(self):
        for code in release.HELPER_ERROR_CODES:
            with self.subTest(code=code):
                coordinator = self.coordinator({'Status': 'Failed', 'StandardErrorContent': SECRET,
                    'StandardOutputContent': json.dumps({'error_code': code, 'error': SECRET, 'environment': SECRET})})
                log = io.StringIO()
                with redirect_stdout(log), self.assertRaises(RuntimeError) as caught:
                    coordinator.command('combined', 'stage')
                command = coordinator.receipt['commands'][0]
                self.assertEqual(command['error_code'], code)
                self.assertEqual(command['status'], 'Failed')
                self.assertNotIn(SECRET, json.dumps(coordinator.receipt) + log.getvalue() + str(caught.exception))

    def test_arbitrary_helper_output_is_replaced_by_fixed_error_code(self):
        for content in (SECRET, json.dumps({'error': SECRET}), json.dumps({'error_code': SECRET}),
                        json.dumps({'error_code': []}), '[]', 'null', None):
            with self.subTest(content=content):
                coordinator = self.coordinator({'Status': SECRET, 'StandardOutputContent': content,
                                                'StandardErrorContent': SECRET})
                log = io.StringIO()
                with redirect_stdout(log), self.assertRaises(RuntimeError) as caught:
                    coordinator.command('combined', 'activate')
                self.assertEqual(coordinator.receipt['commands'][0]['error_code'], 'helper_failed')
                self.assertEqual(coordinator.receipt['commands'][0]['status'], 'Unknown')
                self.assertNotIn(SECRET, json.dumps(coordinator.receipt) + log.getvalue() + str(caught.exception))

    def test_success_requires_expected_release_and_phase(self):
        for payload in (None, '[]', SECRET, json.dumps({'release_id': RELEASE_ID, 'phase': 'active'}),
                        json.dumps({'release_id': NEW_SHA + '-123-1', 'phase': 'staged'})):
            with self.subTest(payload=payload):
                coordinator = self.coordinator({'Status': 'Success', 'StandardOutputContent': payload})
                with redirect_stdout(io.StringIO()), self.assertRaisesRegex(RuntimeError, 'Unexpected release receipt'):
                    coordinator.command('combined', 'stage')
                self.assertEqual(coordinator.receipt['commands'][0]['error_code'], 'invalid_helper_receipt')
                self.assertNotIn(SECRET, json.dumps(coordinator.receipt))

    def test_staging_failure_records_failure_without_activation(self):
        coordinator = self.coordinator({})
        with patch.object(coordinator, 'command', side_effect=RuntimeError(SECRET)) as command:
            with self.assertRaises(RuntimeError):
                coordinator.deploy()
        self.assertEqual(coordinator.receipt['status'], 'stage_failed')
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args, ('combined', 'stage'))
        self.assertNotIn(SECRET, json.dumps(coordinator.receipt))

    def test_rollback_failure_receipt_never_copies_exception_message(self):
        coordinator = self.coordinator({})
        with patch.object(coordinator, 'command', side_effect=[{}, RuntimeError(SECRET), RuntimeError(SECRET)]):
            with self.assertRaises(RuntimeError):
                coordinator.deploy()
        self.assertEqual(coordinator.receipt['status'], 'rollback_failed')
        self.assertEqual(coordinator.receipt['rollback_errors'], [{'runtime': 'combined', 'error_code': 'rollback_failed'}])
        self.assertNotIn(SECRET, json.dumps(coordinator.receipt))


class WorkflowBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = yaml.load((DIRECTORY.parents[1] / '.github/workflows/checks.yml').read_text(), Loader=yaml.BaseLoader)

    def test_push_and_manual_deploy_require_exact_repository_and_branch(self):
        self.assertIn('push', self.workflow['on'])
        deploy = self.workflow['jobs']['deploy']
        for clause in ("github.repository == '" + release.REPOSITORY + "'",
                       "github.repository_id == '" + release.REPOSITORY_ID + "'",
                       "github.ref == '" + release.BRANCH + "'", "github.event_name == 'push'",
                       "github.event_name == 'workflow_dispatch' && inputs.deploy"):
            self.assertIn(clause, deploy['if'])
        self.assertEqual(set(deploy['needs']), {'test', 'security'})
        self.assertEqual(deploy['env']['EXPECTED_SHA'], '${{ inputs.expected_sha }}')
        self.assertEqual(self.workflow['on']['workflow_dispatch']['inputs']['expected_sha']['required'], 'true')

    def test_release_push_and_manual_runs_cannot_cancel_active_deployment(self):
        cancellation = self.workflow['concurrency']['cancel-in-progress']
        self.assertIn("github.event_name != 'workflow_dispatch'", cancellation)
        self.assertIn("&& github.ref != '" + release.BRANCH + "'", cancellation)
        self.assertEqual(self.workflow['jobs']['deploy']['concurrency'],
                         {'group': 'billing-production-release', 'cancel-in-progress': 'false'})

    def test_preflight_gates_oidc_and_deployment_after_artifact_download(self):
        steps = self.workflow['jobs']['deploy']['steps']
        preflight = next(i for i, step in enumerate(steps) if step.get('id') == 'release')
        download = next(i for i, step in enumerate(steps) if step.get('uses', '').startswith('actions/download-artifact@'))
        self.assertGreater(preflight, download)
        self.assertIn('release.py --preflight', steps[preflight]['run'])
        for executable in ('oidc.py', 'release.py /tmp/billing-release'):
            index = next(i for i, step in enumerate(steps) if executable in step.get('run', ''))
            self.assertGreater(index, preflight)
            self.assertEqual(steps[index]['if'], "steps.release.outputs.deploy == 'true'")


if __name__ == '__main__':
    unittest.main()
