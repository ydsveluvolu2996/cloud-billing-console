#!/usr/bin/env python3
"""Root-owned release executor. SSM accepts only immutable release coordinates, never shell text."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import time

RELEASE = re.compile(r'[a-f0-9]{40}-[0-9]+-[0-9]+')
DIGEST = re.compile(r'[a-f0-9]{64}')
VERSION = re.compile(r'[A-Za-z0-9._+/=-]{1,1024}')
ACTIONS = ('stage', 'activate', 'rollback', 'status')
RUNTIME = ('billing/', 'config/', 'templates/', 'static/')
ADDITIVE_MODULE_SHA256 = 'c51c3b282d83c9b8b1c8a7e44ef77b894c4f4a3543fcd5daf8c91b8156921a64'


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
    with temporary.open('w') as stream:
        stream.write(json.dumps(value, indent=2) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def load_additive_module():
    path = Path(__file__).with_name('additive_migrations.py')
    require(path.is_file() and not path.is_symlink(), 'Installed additive maintenance helper is missing or linked')
    for item in (path, *path.parents):
        info = item.lstat()
        require(not item.is_symlink() and info.st_uid == 0 and not info.st_mode & 0o022,
                'Installed additive maintenance helper must be protected and root-owned')
    require(sha256(path) == ADDITIVE_MODULE_SHA256, 'Installed additive maintenance helper checksum differs')
    spec = importlib.util.spec_from_file_location('installed_additive_migrations',path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stop_builder_unit(unit):
    """Do not freeze untrusted output until its entire build cgroup has exited."""
    require(re.fullmatch(r'billing-build-[a-f0-9]{40}-[0-9]+-[0-9]+-[0-9]+\.service',unit), 'Invalid dependency build unit')
    try:
        run(['systemctl','stop',unit],timeout=30)
    except (subprocess.SubprocessError,OSError):
        pass  # An already-collected transient unit is absent; verify below.
    args = ['systemctl','show','--property=LoadState,ActiveState,ControlGroup',unit]
    try:
        output = run(args,timeout=15)
    except subprocess.CalledProcessError as error:
        output = error.stdout or ''
    state = dict(line.split('=',1) for line in output.splitlines() if '=' in line)
    require(state.get('LoadState') in ('loaded','not-found') and state.get('ActiveState') in ('inactive','failed'),
            'Dependency build cleanup is unconfirmed; inspect its transient unit')
    group = state.get('ControlGroup')
    require(group in ('','/system.slice/'+unit), 'Unexpected dependency build control group')
    if group:
        require(Path('/sys/fs/cgroup/cgroup.controllers').is_file(), 'Dependency freeze requires cgroup v2 verification')
        folder = Path('/sys/fs/cgroup') / group.lstrip('/')
        # Descendant cgroups cannot be created by the sandbox, but include them
        # defensively so an empty parent never hides a remaining worker.
        if folder.exists():
            require(all(not item.read_text().strip() for item in folder.rglob('cgroup.procs')),
                    'Dependency build processes are still running')


def freeze_venv(path):
    """Validate every inode before publishing root-owned runtime dependencies."""
    path = Path(path)
    require(stat.S_ISDIR(path.lstat().st_mode) and not path.is_symlink(), 'Dependency output is not a regular directory')
    entries, pending = [(path,path.lstat())], [path]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as iterator:
            for item in iterator:
                info = item.stat(follow_symlinks=False)
                require(stat.S_ISDIR(info.st_mode) or (stat.S_ISREG(info.st_mode) and info.st_nlink == 1),
                        'Dependency output contains a link or special file')
                child = Path(item.path)
                entries.append((child,info))
                if stat.S_ISDIR(info.st_mode):
                    pending.append(child)
    for item,info in entries:
        flags = os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if stat.S_ISDIR(info.st_mode) else 0)
        descriptor = os.open(item,flags)
        try:
            actual = os.fstat(descriptor)
            require((actual.st_dev,actual.st_ino,actual.st_mode,actual.st_nlink) ==
                    (info.st_dev,info.st_ino,info.st_mode,info.st_nlink), 'Dependency output changed while being frozen')
            os.fchown(descriptor,0,0)
            os.fchmod(descriptor,0o755 if stat.S_ISDIR(info.st_mode) or info.st_mode & 0o111 else 0o644)
        finally:
            os.close(descriptor)


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
    paths += ['deploy/database-roles.sql', 'deploy/user-administration.sql', 'deploy/activation-requests.sql', 'deploy/onboarding-worker/activation.service', 'deploy/onboarding-worker/install.py', 'compose.yaml', 'deploy/collector.service'] + ['deploy/single-ec2/metadata_guard.py', 'deploy/single-ec2/metadata-guard.service', 'deploy/single-ec2/docker-metadata.conf', 'deploy/single-ec2/collector.conf']
    actual = {name: sha256(source / name) for name in paths}
    if actual == expected:
        return None
    # Only installed, hash-pinned code may interpret an artifact's migration.
    # It parses the candidate AST and never imports its Python or Django apps.
    return load_additive_module().plan_addition(source,expected,actual)


class Agent:
    def __init__(self, config, release_id, version, digest):
        require(RELEASE.fullmatch(release_id), 'Invalid release ID')
        require(VERSION.fullmatch(version) and version != 'null', 'A versioned S3 manifest is required')
        require(DIGEST.fullmatch(digest), 'Invalid manifest digest')
        require(config['runtime'] in ('web', 'collector', 'combined'), 'Unknown runtime')
        self.config = config
        self.release_id, self.version, self.digest = release_id, version, digest
        self.sha = release_id.split('-')[0]
        self.root = Path(config['root'])
        self.stage = Path(config['releases']) / release_id
        self.stage.mkdir(parents=True, exist_ok=True)
        self.journal = self.stage / 'state.json'
        self.current = self.root / '.deployment/github-release-current.json'
        self.prefix = 'releases/github/' + release_id + '/'

    @property
    def has_web(self):
        return self.config['runtime'] in ('web', 'combined')

    @property
    def has_collector(self):
        return self.config['runtime'] in ('collector', 'combined')

    def activation_service(self, action):
        # Optional until the separately reviewed onboarding bootstrap is installed.
        if self.has_collector and Path('/etc/systemd/system/cloud-billing-activation.service').exists():
            # A rollback can restore a release from before the activation worker existed.
            if action in ('start', 'is-active') and not (self.root / 'billing/management/commands/process_activations.py').is_file():
                return
            run(['systemctl', action, 'cloud-billing-activation'])

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
            return {'phase': 'new', 'runtime': self.config['runtime'], 'release_id': self.release_id, 'manifest_version': self.version, 'manifest_sha256': self.digest}
        state = json.loads(self.journal.read_text())
        if self.config['runtime'] == 'combined':
            require(state.get('runtime') == 'combined', 'Release predates single-EC2 migration; use the preserved maintenance recovery procedure')
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
        wanted = ['source.tar.gz']
        if self.has_web:
            wanted += ['images.json', 'images.tar.gz']
        if self.has_collector:
            wanted += ['wheels.tar.gz']
        require(shutil.disk_usage(self.stage).free > 3 * sum(artifacts[n]['size'] for n in wanted) + 1_000_000_000, 'Not enough free disk for a safe release')
        for name in wanted:
            entry = artifacts[name]
            require(0 < entry['size'] < 3_000_000_000, 'Invalid artifact size')
            self.download(name, entry['version_id'], entry['sha256'])
        source = self.stage / 'source'
        safe_extract(self.stage / 'source.tar.gz', source)
        addition = compatible(source, self.config['compatibility'])
        if addition:
            require(self.config['runtime'] == 'combined', 'Automatic additive migrations require the combined runtime')
            state['additive_migration'] = addition
        if self.has_web:
            run(['docker', 'image', 'load', '--input', str(self.stage / 'images.tar.gz')])
            state['image_id'] = self.verify_images(json.loads((self.stage / 'images.json').read_text()))
        if self.has_collector:
            safe_extract(self.stage / 'wheels.tar.gz', self.stage / 'wheels')
            self.build_collector_venv(source)
        self.save(state, 'staged')
        return state

    def build_collector_venv(self, source):
        venv = self.stage / 'venv'
        require(not venv.exists() and not venv.is_symlink(), 'Inspect the previous dependency build before retrying')
        venv.mkdir(mode=0o700)
        os.chown(venv,65534,65534,follow_symlinks=False)
        # Input directories become traversable, never writable by the builder.
        for folder in (self.stage.parent,self.stage,source,self.stage/'wheels'):
            folder.chmod(0o755)
        environment = ['/usr/bin/env','-i','PATH=/usr/bin:/bin','HOME=/nonexistent',
                       'PYTHONNOUSERSITE=1','PYTHONDONTWRITEBYTECODE=1','PIP_CONFIG_FILE=/dev/null',
                       'PIP_DISABLE_PIP_VERSION_CHECK=1','AWS_EC2_METADATA_DISABLED=true']
        privilege_drop = ['/usr/bin/setpriv','--reuid=65534','--regid=65534','--clear-groups',
                          '--inh-caps=-all','--ambient-caps=-all','--bounding-set=-all','--no-new-privs']
        commands = [
            ['/usr/bin/python3','-I','-m','venv','--copies',str(venv)],
            # CPython creates this redundant alias even with --copies. Remove it
            # unprivileged before pip; frozen dependency trees allow no symlinks.
            ['/usr/bin/unlink',str(venv/'lib64')],
            [str(venv/'bin/python'),'-I','-m','pip','install','--no-index','--no-cache-dir','--only-binary=:all:',
             '--find-links',str(self.stage/'wheels'),'-r',str(source/'requirements.txt')],
            [str(venv/'bin/python'),'-I','-m','pip','check'],
        ]
        properties = ['PrivateNetwork=yes','PrivateTmp=yes','PrivateDevices=yes','ProtectSystem=strict',
                      'ProtectHome=yes','NoNewPrivileges=yes','ProtectKernelTunables=yes','ProtectKernelModules=yes',
                      'ProtectControlGroups=yes','RestrictSUIDSGID=yes','RestrictAddressFamilies=AF_UNIX',
                      'CapabilityBoundingSet=CAP_SETUID CAP_SETGID CAP_SETPCAP','AmbientCapabilities=',
                      'ReadWritePaths='+str(venv),'KillMode=control-group','TimeoutStopSec=10s',
                      'RuntimeMaxSec=300s','MemoryMax=1G','TasksMax=64','UMask=0077']
        for index,command in enumerate(commands):
            unit = 'billing-build-'+self.release_id+'-'+str(index)+'.service'
            args = ['systemd-run','--quiet','--wait','--pipe','--collect','--unit='+unit,
                    '--working-directory='+str(venv)]
            args.extend('--property='+value for value in properties)
            try:
                run(args+privilege_drop+environment+command,timeout=360)
            finally:
                stop_builder_unit(unit)
        freeze_venv(venv)

    def health(self):
        if self.has_collector:
            self.activation_service('is-active')
            run(['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--uid=billing-collector',
                 '--working-directory=' + str(self.root), '--property=EnvironmentFile=/etc/cloud-billing/collector.env',
                 str(self.root / '.venv/bin/python'), 'manage.py', 'verify_runtime'])
            run(['systemctl', 'is-active', '--quiet', 'cloud-billing-collector'])
        if not self.has_web:
            return
        if self.config['runtime'] == 'combined':
            run(['/usr/local/sbin/cloud-billing-metadata-guard', '--check'])
        run(['docker', 'compose', 'exec', '-T', 'app', 'python', 'manage.py', 'verify_runtime'], cwd=self.root)
        origin = 'https://' + self.config['hostname']
        args = ['curl', '--silent', '--show-error', '--max-time', '10', '--resolve', self.config['hostname'] + ':443:127.0.0.1']
        run(args + ['--fail', origin + '/health/'])
        require(run(args + ['--output', '/dev/null', '--write-out', '%{http_code}', origin + '/']).strip() == '302', 'Anonymous dashboard access must redirect to login')
        login = run(args + ['--fail', origin + '/login/'])
        require('name="token"' in login and 'one-time-code' in login, 'MFA login control is missing')
        run(args + ['--fail', origin + '/static/app.js'])

    def require_no_database_maintenance(self):
        marker = self.root / '.deployment/postgres-maintenance.json'
        if marker.exists():
            phase = json.loads(marker.read_text()).get('phase')
            require(phase in ('rehearsed', 'writers-reopened'),
                    'Database maintenance is active; finish maintenance before a release')

    def activate(self):
        self.require_no_database_maintenance()
        state = self.state()
        if state['phase'] == 'active':
            require(self.current.exists() and json.loads(self.current.read_text())['release_id'] == self.release_id, 'A newer release is active')
            self.health()
            return state
        require(state['phase'] == 'staged', 'Release must pass staging before activation')
        if state.get('additive_migration'):
            receipt = load_additive_module().apply(self.root,self.stage,self.release_id,
                        state['additive_migration'],self.config,run,write_json)
            state['additive_receipt'] = {'phase':receipt['phase'],'backup_stamp':receipt['backup_stamp'],
                                         'migration':receipt['plan']['name']}
            self.save(state,'staged')
            # The verified migration file is now part of the old runtime backup.
            # A code rollback therefore retains both its file and its DB column.
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
        if self.has_web:
            shutil.copy2(self.root / '.env', backup / 'environment')
            if not state.get('additive_receipt'):
                run(['bash', str(self.root / 'deploy/backup.sh')], cwd=self.root)
        if self.has_collector:
            venv = self.root / '.venv'
            state['previous_venv_link'] = str(venv.readlink()) if venv.is_symlink() else None
            require(venv.exists(), 'Collector virtualenv is missing')
        self.save(state, 'activating')
        write_json(self.current, {'release_id': self.release_id, 'phase': 'activating'})
        try:
            if self.has_collector:
                self.activation_service('stop')
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
            if self.has_web:
                env = self.root / '.env'
                text = env.read_text()
                require(len(re.findall(r'^BILLING_APP_IMAGE=.*$', text, re.M)) == 1, 'Expected exactly one web image setting')
                text = re.sub(r'^BILLING_APP_IMAGE=.*$', 'BILLING_APP_IMAGE=' + state['image_id'], text, flags=re.M)
                temporary = env.with_suffix('.release')
                temporary.write_text(text)
                temporary.chmod(0o600)
                temporary.replace(env)
                run(['docker', 'compose', 'up', '-d', '--no-build', '--no-deps', 'app'], cwd=self.root)
            if self.has_collector:
                venv = self.root / '.venv'
                if venv.is_symlink():
                    venv.unlink()
                else:
                    venv.rename(backup / 'venv')
                venv.symlink_to(self.stage / 'venv', target_is_directory=True)
                run(['systemctl', 'start', 'cloud-billing-collector'])
                self.activation_service('start')
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

    def require_database_rollback_compatible(self, backup):
        def database_settings(path):
            text = path.read_text()
            result = {}
            for key in ('BILLING_POSTGRES_IMAGE', 'BILLING_POSTGRES_VOLUME'):
                values = re.findall(r'^' + key + r'=(.*)$', text, re.M)
                require(len(values) <= 1, 'Ambiguous database configuration')
                result[key] = values[0] if values else None
            return result
        require(database_settings(backup / 'environment') == database_settings(self.root / '.env'),
                'Database configuration changed after this release; use maintenance recovery')

    def rollback(self):
        self.require_no_database_maintenance()
        state = self.state()
        if state['phase'] == 'rolled_back':
            self.health()
            return state
        if state['phase'] in ('new', 'staged'):
            return state
        require(self.current.exists() and json.loads(self.current.read_text())['release_id'] == self.release_id, 'Refusing to roll back a different/newer release')
        backup = self.stage / 'backup'
        if self.has_web:
            self.require_database_rollback_compatible(backup)
        if self.has_collector:
            self.activation_service('stop')
            run(['systemctl', 'stop', 'cloud-billing-collector'])
        for item in state['files']:
            target = self.root / item['name']
            if item['existed']:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(backup / item['name'], target)
            else:
                target.unlink(missing_ok=True)
        if self.has_web:
            shutil.copy2(backup / 'environment', self.root / '.env')
            run(['docker', 'compose', 'up', '-d', '--no-build', '--no-deps', 'app'], cwd=self.root)
        if self.has_collector:
            venv = self.root / '.venv'
            if venv.is_symlink():
                venv.unlink()
            if (backup / 'venv').exists():
                (backup / 'venv').rename(venv)
            elif state['previous_venv_link']:
                venv.symlink_to(state['previous_venv_link'], target_is_directory=True)
            run(['systemctl', 'start', 'cloud-billing-collector'])
            self.activation_service('start')
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
        code = ('validation_failed' if isinstance(error,ValueError) else
                'timeout' if isinstance(error,subprocess.TimeoutExpired) else
                'command_failed' if isinstance(error,(subprocess.CalledProcessError,OSError)) else 'internal_error')
        print(json.dumps({'error_code':code,'error': str(error) if isinstance(error, ValueError) else type(error).__name__}))
        raise SystemExit(1)
