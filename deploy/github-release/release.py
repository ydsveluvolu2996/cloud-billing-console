#!/usr/bin/env python3
"""Upload an exact CI release and coordinate both fixed SSM targets with rollback."""
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time

import boto3
from botocore.exceptions import ClientError

ACCOUNT = '582287676741'
REGION = 'ap-south-1'
BUCKET = 'cloud-billing-console-artifacts-1p8h79kjwyyw'
REPOSITORY_ID = '1361643722'
INSTANCES = {'web': 'i-0cae3cd32c80de891', 'collector': 'i-0bba5908e62ce509e'}
DOCUMENT = 'CloudBilling-GitHubRelease'
BRANCH = 'refs/heads/codex/billing-security-portfolio'


def digest(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def check_context(env):
    if env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch' or env.get('GITHUB_REF') != BRANCH:
        raise ValueError('Release must be manually dispatched from the protected release branch')
    sha = env['GITHUB_SHA']
    if not re.fullmatch('[a-f0-9]{40}', sha) or env.get('EXPECTED_SHA') != sha:
        raise ValueError('The requested SHA no longer matches this run; dispatch the intended exact release again')
    if env.get('GITHUB_REPOSITORY_ID') != REPOSITORY_ID:
        raise ValueError('Wrong repository')
    return sha


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
        self.receipt.setdefault('commands', []).append({'runtime': runtime, 'action': action, 'command_id': command_id})
        print(json.dumps(self.receipt['commands'][-1]), flush=True)
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
                output = json.loads(result['StandardOutputContent'].strip())
                expected_phase = {'stage': 'staged', 'activate': 'active'}.get(action)
                if output.get('release_id') != self.release_id or (expected_phase and output.get('phase') != expected_phase):
                    raise RuntimeError('Unexpected release receipt')
                return output
            if status not in ('Pending', 'InProgress', 'Delayed', 'Cancelling'):
                # Only the fixed helper's sanitized JSON is exposed; no environment or raw stderr.
                raise RuntimeError(runtime + ' ' + action + ' failed: ' + status + ' (SSM ' + command_id + ')')
            time.sleep(5)
        raise TimeoutError('SSM release command did not complete; inspect command ' + command_id)

    def deploy(self):
        for runtime in ('collector', 'web'):
            self.command(runtime, 'stage')
        attempted = []
        try:
            for runtime in ('collector', 'web'):
                attempted.append(runtime)
                self.command(runtime, 'activate')
            self.receipt['status'] = 'active'
        except Exception:
            failures = []
            for runtime in reversed(attempted):
                try:
                    self.command(runtime, 'rollback')
                except Exception as error:
                    failures.append(runtime + ': ' + str(error))
            self.receipt['status'] = 'rollback_failed' if failures else 'rolled_back'
            self.receipt['rollback_errors'] = failures
            raise


def main(directory):
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
        Path('release-receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as summary:
            summary.write('Billing release `' + sha + '` — **' + receipt['status'] + '**\n')


if __name__ == '__main__':
    main(sys.argv[1])
