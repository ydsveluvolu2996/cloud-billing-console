#!/usr/bin/env python3
"""Root-owned release executor. SSM accepts only immutable release coordinates, never shell text."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import time

RELEASE = re.compile(r'[a-f0-9]{40}-[0-9]+-[0-9]+')
DIGEST = re.compile(r'[a-f0-9]{64}')
VERSION = re.compile(r'[A-Za-z0-9._+/=-]{1,1024}')
ACTIONS = ('stage', 'activate', 'rollback', 'status')
RUNTIME = ('billing/', 'config/', 'templates/', 'static/')


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout


def write_json(path, value):
    path = Path(path)
    temporary = path.with_suffix('.new')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def safe_extract(path, destination, limit=2_000_000_000):
    """Reject traversal, links, devices and duplicate members before creating files."""
    with tarfile.open(path) as archive:
        members = archive.getmembers()
        seen = set()
        for member in members:
            name = PurePosixPath(member.name)
            require(not name.is_absolute() and '..' not in name.parts and '\\' not in member.name, 'Unsafe archive path')
            require(member.isfile() or member.isdir(), 'Archive links/devices are not allowed')
            require(str(name) not in seen, 'Duplicate archive member')
            seen.add(str(name))
        require(sum(m.size for m in members) <= limit, 'Expanded archive exceeds release limit')
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        for member in members:
            target = destination / member.name
            require(not target.is_symlink() and not any(p.is_symlink() for p in target.parents), 'Archive destination contains a symlink')
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, target.open('wb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o644)


def compatible(source, expected):
    paths = sorted(str(p.relative_to(source)) for p in (source / 'billing/migrations').glob('*.py'))
    paths += ['deploy/database-roles.sql', 'deploy/user-administration.sql', 'compose.yaml', 'deploy/collector.service']
    actual = {name: sha256(source / name) for name in paths}
    require(actual == expected, 'Schema, database policy or service configuration changed; a separate maintenance deployment is required')


class Agent:
    def __init__(self, config, release_id, version, digest):
        require(RELEASE.fullmatch(release_id), 'Invalid release ID')
        require(VERSION.fullmatch(version) and version != 'null', 'A versioned S3 manifest is required')
        require(DIGEST.fullmatch(digest), 'Invalid manifest digest')
        self.config = config
        self.release_id, self.version, self.digest = release_id, version, digest
        self.sha = release_id.split('-')[0]
        self.root = Path(config['root'])
        self.stage = Path(config['releases']) / release_id
        self.stage.mkdir(parents=True, exist_ok=True)
        self.journal = self.stage / 'state.json'
        self.current = self.root / '.deployment/github-release-current.json'
        self.prefix = 'releases/github/' + release_id + '/'

    def download(self, name, version, digest):
        require(re.fullmatch(r'[a-z0-9.-]+', name), 'Invalid artifact name')
        require(VERSION.fullmatch(version) and version != 'null', 'Artifact must be versioned')
        require(DIGEST.fullmatch(digest), 'Invalid artifact digest')
        target = self.stage / name
        if not target.exists() or sha256(target) != digest:
            run([self.config['aws_cli'], 's3api', 'get-object', '--region', self.config['region'],
                 '--bucket', self.config['bucket'], '--key', self.prefix + name, '--version-id', version,
                 '--expected-bucket-owner', self.config['account_id'], str(target)])
        require(sha256(target) == digest, 'Release artifact checksum mismatch')
        return target

    def state(self):
        if not self.journal.exists():
            return {'phase': 'new', 'release_id': self.release_id, 'manifest_version': self.version, 'manifest_sha256': self.digest}
        state = json.loads(self.journal.read_text())
        require((state['manifest_version'], state['manifest_sha256']) == (self.version, self.digest), 'Release coordinates changed')
        return state

    def save(self, state, phase):
        state['phase'] = phase
        write_json(self.journal, state)

    def verify_images(self, expected):
        tag = 'cloud-billing-app:' + self.sha
        item = next(i for i in expected if tag in i['RepoTags'])
        actual = json.loads(run(['docker', 'image', 'inspect', tag]))[0]
        require(actual['Architecture'] == item['Architecture'] == 'amd64', 'Wrong image architecture')
        require(actual['Config']['User'] in ('app', '10001', '10001:10001'), 'Web image must run as the application user')
        require(actual['RootFS']['Layers'] == item['RootFS']['Layers'], 'Image layers differ from CI')
        exported = self.stage / 'verify-image.tar'
        run(['docker', 'image', 'save', '--output', str(exported), tag])
        with tarfile.open(exported) as archive:
            entry = next(i for i in json.load(archive.extractfile('manifest.json')) if tag in i['RepoTags'])
            raw = archive.extractfile(entry['Config']).read()
            require('sha256:' + hashlib.sha256(raw).hexdigest() == item['Id'], 'Image configuration differs from CI')
        exported.unlink()
        return actual['Id']

    def stage_release(self):
        state = self.state()
        if state['phase'] in ('staged', 'active'):
            return state
        require(state['phase'] == 'new', 'Inspect the previous release attempt before retrying')
        manifest = json.loads(self.download('manifest.json', self.version, self.digest).read_text())
        require(manifest['release_id'] == self.release_id and manifest['sha'] == self.sha, 'Wrong release manifest')
        require(str(manifest['repository_id']) == str(self.config['repository_id']), 'Wrong repository')
        require(manifest['account_id'] == self.config['account_id'], 'Wrong hosting account')
        require(manifest['instances'][self.config['runtime']] == self.config['instance_id'], 'Wrong target instance')
        require(manifest.get('executor_sha256') == sha256(Path(__file__)), 'The release executor changed; update the reviewed root-owned helper before deploying')
        artifacts = manifest['artifacts']
        wanted = ['source.tar.gz', 'images.json', 'images.tar.gz'] if self.config['runtime'] == 'web' else ['source.tar.gz', 'wheels.tar.gz']
        require(shutil.disk_usage(self.stage).free > 3 * sum(artifacts[n]['size'] for n in wanted) + 1_000_000_000, 'Not enough free disk for a safe release')
        for name in wanted:
            entry = artifacts[name]
            require(0 < entry['size'] < 3_000_000_000, 'Invalid artifact size')
            self.download(name, entry['version_id'], entry['sha256'])
        source = self.stage / 'source'
        safe_extract(self.stage / 'source.tar.gz', source)
        compatible(source, self.config['compatibility'])
        if self.config['runtime'] == 'web':
            run(['docker', 'image', 'load', '--input', str(self.stage / 'images.tar.gz')])
            state['image_id'] = self.verify_images(json.loads((self.stage / 'images.json').read_text()))
        else:
            safe_extract(self.stage / 'wheels.tar.gz', self.stage / 'wheels')
            venv = self.stage / 'venv'
            previous_umask = os.umask(0o022)
            try:
                run(['/usr/bin/python3', '-m', 'venv', str(venv)])
                run([str(venv / 'bin/python'), '-m', 'pip', 'install', '--no-index', '--no-cache-dir',
                     '--find-links', str(self.stage / 'wheels'), '-r', str(source / 'requirements.txt')])
            finally:
                os.umask(previous_umask)
            run([str(venv / 'bin/python'), '-m', 'pip', 'check'])
            # Service can traverse/read immutable root-owned release code and dependencies.
            for folder in [self.stage.parent, self.stage, venv]:
                folder.chmod(0o755)
        self.save(state, 'staged')
        return state

    def health(self):
        if self.config['runtime'] == 'collector':
            run(['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--uid=billing-collector',
                 '--working-directory=' + str(self.root), '--property=EnvironmentFile=/etc/cloud-billing/collector.env',
                 str(self.root / '.venv/bin/python'), 'manage.py', 'verify_runtime'])
            run(['systemctl', 'is-active', '--quiet', 'cloud-billing-collector'])
            return
        run(['docker', 'compose', 'exec', '-T', 'app', 'python', 'manage.py', 'verify_runtime'], cwd=self.root)
        origin = 'https://' + self.config['hostname']
        args = ['curl', '--silent', '--show-error', '--max-time', '10', '--resolve', self.config['hostname'] + ':443:127.0.0.1']
        run(args + ['--fail', origin + '/health/'])
        require(run(args + ['--output', '/dev/null', '--write-out', '%{http_code}', origin + '/']).strip() == '302', 'Anonymous dashboard access must redirect to login')
        login = run(args + ['--fail', origin + '/login/'])
        require('name="token"' in login and 'one-time-code' in login, 'MFA login control is missing')
        run(args + ['--fail', origin + '/static/app.js'])

    def activate(self):
        state = self.state()
        if state['phase'] == 'active':
            require(self.current.exists() and json.loads(self.current.read_text())['release_id'] == self.release_id, 'A newer release is active')
            self.health()
            return state
        require(state['phase'] == 'staged', 'Release must pass staging before activation')
        backup = self.stage / 'backup'
        require(not backup.exists(), 'Existing rollback data must be inspected')
        backup.mkdir(mode=0o700)
        state['previous_current'] = json.loads(self.current.read_text()) if self.current.exists() else None
        source = self.stage / 'source'
        files = sorted(str(p.relative_to(source)) for p in source.rglob('*') if p.is_file() and (str(p.relative_to(source)).startswith(RUNTIME) or str(p.relative_to(source)) in ('manage.py', 'requirements.txt')))
        previous_files = set()
        for directory in RUNTIME:
            previous_files.update(str(p.relative_to(self.root)) for p in (self.root / directory).rglob('*')
                                  if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
        state['files'] = []
        for name in sorted(set(files) | previous_files):
            target = self.root / name
            require(not target.is_symlink() and not any(p.is_symlink() for p in target.parents if p != self.root.parent), 'Unexpected runtime symlink')
            state['files'].append({'name': name, 'existed': target.exists()})
            if target.exists():
                saved = backup / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
        if self.config['runtime'] == 'web':
            shutil.copy2(self.root / '.env', backup / 'environment')
            run(['bash', str(self.root / 'deploy/backup.sh')], cwd=self.root)
        else:
            venv = self.root / '.venv'
            state['previous_venv_link'] = str(venv.readlink()) if venv.is_symlink() else None
            require(venv.exists(), 'Collector virtualenv is missing')
        self.save(state, 'activating')
        write_json(self.current, {'release_id': self.release_id, 'phase': 'activating'})
        try:
            if self.config['runtime'] == 'collector':
                run(['systemctl', 'stop', 'cloud-billing-collector'])
            for name in previous_files - set(files):
                (self.root / name).unlink()
            for name in files:
                target = self.root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                for parent in target.parents:
                    if parent == self.root:
                        break
                    parent.chmod(0o755)
                shutil.copy2(source / name, target)
                target.chmod(0o644)
            if self.config['runtime'] == 'web':
                env = self.root / '.env'
                text = env.read_text()
                require(len(re.findall(r'^BILLING_APP_IMAGE=.*$', text, re.M)) == 1, 'Expected exactly one web image setting')
                text = re.sub(r'^BILLING_APP_IMAGE=.*$', 'BILLING_APP_IMAGE=' + state['image_id'], text, flags=re.M)
                temporary = env.with_suffix('.release')
                temporary.write_text(text)
                temporary.chmod(0o600)
                temporary.replace(env)
                run(['docker', 'compose', 'up', '-d', '--no-build', '--no-deps', 'app'], cwd=self.root)
            else:
                venv = self.root / '.venv'
                if venv.is_symlink():
                    venv.unlink()
                else:
                    venv.rename(backup / 'venv')
                venv.symlink_to(self.stage / 'venv', target_is_directory=True)
                run(['systemctl', 'start', 'cloud-billing-collector'])
            for attempt in range(20):
                try:
                    self.health()
                    break
                except (ValueError, subprocess.CalledProcessError):
                    if attempt == 19:
                        raise
                    time.sleep(3)
            self.save(state, 'active')
            write_json(self.current, {'release_id': self.release_id, 'phase': 'active', 'sha': self.sha})
            return state
        except Exception:
            self.rollback()
            raise

    def rollback(self):
        state = self.state()
        if state['phase'] == 'rolled_back':
            self.health()
            return state
        if state['phase'] in ('new', 'staged'):
            return state
        require(self.current.exists() and json.loads(self.current.read_text())['release_id'] == self.release_id, 'Refusing to roll back a different/newer release')
        backup = self.stage / 'backup'
        if self.config['runtime'] == 'collector':
            run(['systemctl', 'stop', 'cloud-billing-collector'])
        for item in state['files']:
            target = self.root / item['name']
            if item['existed']:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup / item['name'], target)
            else:
                target.unlink(missing_ok=True)
        if self.config['runtime'] == 'web':
            shutil.copy2(backup / 'environment', self.root / '.env')
            run(['docker', 'compose', 'up', '-d', '--no-build', '--no-deps', 'app'], cwd=self.root)
        else:
            venv = self.root / '.venv'
            if venv.is_symlink():
                venv.unlink()
            if (backup / 'venv').exists():
                (backup / 'venv').rename(venv)
            elif state['previous_venv_link']:
                venv.symlink_to(state['previous_venv_link'], target_is_directory=True)
            run(['systemctl', 'start', 'cloud-billing-collector'])
        if state['previous_current']:
            write_json(self.current, state['previous_current'])
        else:
            self.current.unlink(missing_ok=True)
        self.save(state, 'rolled_back')
        self.health()
        return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=ACTIONS)
    parser.add_argument('release_id')
    parser.add_argument('manifest_version')
    parser.add_argument('manifest_sha256')
    args = parser.parse_args()
    require(os.geteuid() == 0, 'Release executor requires root')
    os.umask(0o077)
    config_path = Path('/etc/cloud-billing/release.json')
    require(config_path.stat().st_uid == 0 and config_path.stat().st_mode & 0o022 == 0, 'Release configuration must be root-owned and not writable by the runtime')
    config = json.loads(config_path.read_text())
    with open('/run/lock/cloud-billing-release.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        agent = Agent(config, args.release_id, args.manifest_version, args.manifest_sha256)
        method = {'stage': agent.stage_release, 'activate': agent.activate, 'rollback': agent.rollback, 'status': agent.state}[args.action]
        state = method()
        print(json.dumps({'runtime': config['runtime'], 'release_id': args.release_id, 'phase': state['phase']}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # Subprocess output can include runtime configuration; retain no raw stderr in SSM/GitHub logs.
        print(json.dumps({'error': str(error) if isinstance(error, ValueError) else type(error).__name__}))
        raise SystemExit(1)
