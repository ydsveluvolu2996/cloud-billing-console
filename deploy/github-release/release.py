#!/usr/bin/env python3
"""Upload an exact CI release and coordinate both runtimes on one fixed SSM target with rollback."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.request

import boto3
from botocore.exceptions import ClientError

ACCOUNT = '582287676741'
REGION = 'ap-south-1'
BUCKET = 'cloud-billing-console-artifacts-1p8h79kjwyyw'
REPOSITORY_ID = '1361643722'
REPOSITORY = 'ydsveluvolu2996/cloud-billing-console'
INSTANCES = {'combined': 'i-0cae3cd32c80de891'}
DOCUMENT = 'CloudBilling-GitHubRelease'
BRANCH = 'refs/heads/codex/billing-security-portfolio'
BRANCH_API = 'https://api.github.com/repos/' + REPOSITORY + '/git/ref/' + BRANCH.removeprefix('refs/')
HELPER_ERROR_CODES = frozenset({'validation_failed', 'command_failed', 'timeout', 'internal_error'})


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def check_context(env):
    event = env.get('GITHUB_EVENT_NAME')
    if event not in ('push', 'workflow_dispatch') or env.get('GITHUB_REF') != BRANCH:
        raise ValueError('Release requires a push or manual dispatch on the protected release branch')
    if env.get('GITHUB_REPOSITORY_ID') != REPOSITORY_ID or env.get('GITHUB_REPOSITORY') != REPOSITORY:
        raise ValueError('Wrong repository')
    sha = env.get('GITHUB_SHA', '')
    if not re.fullmatch('[a-f0-9]{40}', sha):
        raise ValueError('Release requires a full commit SHA')
    if event == 'workflow_dispatch' and env.get('EXPECTED_SHA') != sha:
        raise ValueError('The requested SHA no longer matches this run; dispatch the intended exact release again')
    return sha


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # The repository API is fixed; never forward its bearer token to a redirect.
        return None


def current_branch_sha(env):
    token = env.get('GITHUB_TOKEN')
    if not token:
        raise ValueError('A GitHub token is required to verify the release branch')
    request = urllib.request.Request(BRANCH_API, headers={
        'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'billing-release-coordinator',
    })
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=30) as response:
            result = json.load(response)
        if result['ref'] != BRANCH or result['object']['type'] != 'commit':
            raise ValueError('Unexpected reference')
        sha = result['object']['sha']
        if not isinstance(sha, str) or not re.fullmatch('[a-f0-9]{40}', sha):
            raise ValueError('Invalid reference SHA')
    except Exception:
        # HTTP errors and response bodies may contain credentials or arbitrary text.
        raise RuntimeError('Cannot verify the current release branch; deployment stopped') from None
    return sha


def write_receipt(receipt, env):
    Path('release-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    if env.get('GITHUB_STEP_SUMMARY'):
        with Path(env['GITHUB_STEP_SUMMARY']).open('a') as summary:
            summary.write('Billing release `' + receipt['sha'] + '` — **' + receipt['status'] + '**\n')


def preflight(env):
    """Authorize this tested commit before requesting credentials or touching AWS."""
    sha = check_context(env)
    deploy = True
    if env['GITHUB_EVENT_NAME'] == 'push':
        head = current_branch_sha(env)
        if head != sha:
            deploy = False
            write_receipt({'sha': sha, 'status': 'skipped_superseded', 'current_branch_sha': head}, env)
            print('Skipping superseded release push; no AWS deployment requested.', flush=True)
    if env.get('GITHUB_OUTPUT'):
        with Path(env['GITHUB_OUTPUT']).open('a') as output:
            output.write('deploy=' + str(deploy).lower() + '\n')
    return deploy


def helper_error_code(result):
    """Expose only the fixed helper's known error codes, never subprocess output."""
    try:
        output = json.loads(result.get('StandardOutputContent', ''))
        code = output.get('error_code') if isinstance(output, dict) else None
        if isinstance(code, str) and code in HELPER_ERROR_CODES:
            return code
    except (ValueError, TypeError):
        pass
    return 'helper_failed'


class Release:
    def __init__(self, ssm, release_id, version, checksum, receipt):
        self.ssm, self.release_id, self.version, self.checksum = ssm, release_id, version, checksum
        self.receipt = receipt

    def command(self, runtime, action):
        response = self.ssm.send_command(
            InstanceIds=[INSTANCES[runtime]], DocumentName=DOCUMENT,
            Comment='Billing release ' + action + ' ' + self.release_id,
            Parameters={'action': [action], 'releaseId': [self.release_id],
                        'manifestVersion': [self.version], 'manifestSha256': [self.checksum]}, TimeoutSeconds=120)
        command_id = response['Command']['CommandId']
        command_receipt = {'runtime': runtime, 'action': action, 'command_id': command_id}
        self.receipt.setdefault('commands', []).append(command_receipt)
        print(json.dumps(command_receipt), flush=True)
        deadline = time.monotonic() + 1200
        while time.monotonic() < deadline:
            try:
                result = self.ssm.get_command_invocation(CommandId=command_id, InstanceId=INSTANCES[runtime])
            except ClientError as error:
                if error.response['Error']['Code'] != 'InvocationDoesNotExist':
                    raise
                time.sleep(5)
                continue
            status = result['Status']
            if status == 'Success':
                try:
                    output = json.loads(result['StandardOutputContent'])
                except (ValueError, TypeError, KeyError):
                    output = None
                expected_phase = {'stage': 'staged', 'activate': 'active'}.get(action)
                if not isinstance(output, dict) or output.get('release_id') != self.release_id or (expected_phase and output.get('phase') != expected_phase):
                    command_receipt.update(status='InvalidReceipt', error_code='invalid_helper_receipt')
                    raise RuntimeError('Unexpected release receipt')
                command_receipt['status'] = 'Success'
                return output
            if status not in ('Pending', 'InProgress', 'Delayed', 'Cancelling'):
                safe_status = status if status in ('Failed', 'Cancelled', 'TimedOut') else 'Unknown'
                command_receipt.update(status=safe_status, error_code=helper_error_code(result))
                print(json.dumps(command_receipt), flush=True)
                raise RuntimeError(runtime + ' ' + action + ' failed: ' + command_receipt['error_code'] + ' (SSM ' + command_id + ')')
            time.sleep(5)
        command_receipt.update(status='TimedOut', error_code='command_timeout')
        raise TimeoutError('SSM release command did not complete; inspect command ' + command_id)

    def deploy(self):
        try:
            for runtime in INSTANCES:
                self.command(runtime, 'stage')
        except Exception:
            self.receipt['status'] = 'stage_failed'
            raise
        attempted = []
        try:
            for runtime in INSTANCES:
                attempted.append(runtime)
                self.command(runtime, 'activate')
            self.receipt['status'] = 'active'
        except Exception:
            failures = []
            for runtime in reversed(attempted):
                try:
                    self.command(runtime, 'rollback')
                except Exception:
                    failures.append({'runtime': runtime, 'error_code': 'rollback_failed'})
            self.receipt['status'] = 'rollback_failed' if failures else 'rolled_back'
            self.receipt['rollback_errors'] = failures
            raise


def main(directory):
    if not preflight(os.environ):
        return
    sha = check_context(os.environ)
    session = boto3.Session(region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise ValueError('Wrong hosting account')
    release_id = sha + '-' + os.environ['GITHUB_RUN_ID'] + '-' + os.environ['GITHUB_RUN_ATTEMPT']
    directory = Path(directory)
    checksums = json.loads((directory / 'checksums.json').read_text())
    expected = {'source.tar.gz', 'images.json', 'images.tar.gz', 'wheels.tar.gz'}
    if set(checksums) != expected:
        raise ValueError('Incomplete release artifact')
    s3 = session.client('s3')
    manifest = {'release_id': release_id, 'sha': sha, 'repository_id': REPOSITORY_ID, 'account_id': ACCOUNT,
                'instances': INSTANCES, 'executor_sha256': digest(Path(__file__).with_name('agent.py')), 'artifacts': {}}
    for name in sorted(expected):
        path = directory / name
        if digest(path) != checksums[name]:
            raise ValueError('CI artifact checksum mismatch: ' + name)
        with path.open('rb') as stream:
            response = s3.put_object(Bucket=BUCKET, Key='releases/github/' + release_id + '/' + name,
                                     Body=stream, ServerSideEncryption='AES256', ExpectedBucketOwner=ACCOUNT)
        version = response.get('VersionId')
        if not version or version == 'null':
            raise ValueError('Artifact bucket versioning must be enabled')
        manifest['artifacts'][name] = {'sha256': checksums[name], 'size': path.stat().st_size, 'version_id': version}
    content = json.dumps(manifest, sort_keys=True).encode()
    response = s3.put_object(Bucket=BUCKET, Key='releases/github/' + release_id + '/manifest.json',
                            Body=content, ServerSideEncryption='AES256', ExpectedBucketOwner=ACCOUNT)
    version = response.get('VersionId')
    if not version or version == 'null':
        raise ValueError('Manifest must be versioned')
    checksum = hashlib.sha256(content).hexdigest()
    receipt = {'release_id': release_id, 'sha': sha, 'manifest_version': version, 'manifest_sha256': checksum,
               'status': 'prepared'}
    try:
        Release(session.client('ssm'), release_id, version, checksum, receipt).deploy()
    finally:
        write_receipt(receipt, os.environ)


if __name__ == '__main__':
    if sys.argv[1:] == ['--preflight']:
        preflight(os.environ)
    else:
        main(sys.argv[1])
