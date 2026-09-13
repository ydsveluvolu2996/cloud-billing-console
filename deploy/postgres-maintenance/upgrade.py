#!/usr/bin/env python3
"""Separate, fail-closed PostgreSQL 17 -> 18 maintenance; never deletes a volume."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time

BOOTSTRAP = 'billing_migration_bootstrap'
CATALOG = """SELECT jsonb_build_object(
'roles',(SELECT jsonb_agg(to_jsonb(r) ORDER BY rolname) FROM (SELECT r.rolname,r.rolsuper,r.rolinherit,r.rolcreaterole,r.rolcreatedb,r.rolcanlogin,r.rolreplication,r.rolbypassrls,r.rolconnlimit,r.rolvaliduntil,r.rolconfig,a.rolpassword FROM pg_roles r JOIN pg_authid a USING (oid) WHERE r.rolname !~ '^pg_' AND r.rolname <> 'billing_migration_bootstrap') r),
'schemas',(SELECT jsonb_agg(to_jsonb(r) ORDER BY nspname) FROM (SELECT nspname,pg_get_userbyid(nspowner) owner,ARRAY(SELECT x::text FROM unnest(coalesce(nspacl,acldefault('n',nspowner))) x ORDER BY x::text) nspacl FROM pg_namespace WHERE nspname='public') r),
'database',(SELECT jsonb_build_object('owner',pg_get_userbyid(datdba),'acl',ARRAY(SELECT x::text FROM unnest(coalesce(datacl,acldefault('d',datdba))) x ORDER BY x::text)) FROM pg_database WHERE datname='billing'),
'memberships',(SELECT jsonb_agg(to_jsonb(r) ORDER BY parent,member) FROM (SELECT p.rolname parent,c.rolname member,m.admin_option,m.inherit_option,m.set_option FROM pg_auth_members m JOIN pg_roles p ON p.oid=m.roleid JOIN pg_roles c ON c.oid=m.member) r),
'relations',(SELECT jsonb_agg(to_jsonb(r) ORDER BY nspname,relname) FROM (SELECT n.nspname,c.relname,c.relkind,pg_get_userbyid(c.relowner) owner,c.relrowsecurity,c.relforcerowsecurity,ARRAY(SELECT x::text FROM unnest(coalesce(c.relacl,acldefault(CASE WHEN c.relkind='S' THEN 'S'::"char" ELSE 'r'::"char" END,c.relowner))) x ORDER BY x::text) relacl FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public') r),
'policies',(SELECT jsonb_agg(to_jsonb(p) ORDER BY schemaname,tablename,policyname) FROM pg_policies p WHERE schemaname='public'),
'functions',(SELECT jsonb_agg(to_jsonb(r) ORDER BY signature) FROM (SELECT p.oid::regprocedure::text signature,pg_get_userbyid(p.proowner) owner,p.prosecdef,p.proconfig,ARRAY(SELECT x::text FROM unnest(coalesce(p.proacl,acldefault('f',p.proowner))) x ORDER BY x::text) proacl,ARRAY(SELECT x::text FROM unnest(acldefault('f',p.proowner)) x ORDER BY x::text) default_acl,(SELECT e.extname FROM pg_depend d JOIN pg_extension e ON e.oid=d.refobjid WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.refclassid='pg_extension'::regclass AND d.deptype='e') extension FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public') r),
'extensions',(SELECT jsonb_agg(to_jsonb(r) ORDER BY extname) FROM (SELECT e.extname,e.extversion,pg_get_userbyid(e.extowner) owner,n.nspname namespace FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace) r),
'default_acl',(SELECT jsonb_agg(to_jsonb(r) ORDER BY owner,namespace,defaclobjtype) FROM (SELECT pg_get_userbyid(d.defaclrole) owner,n.nspname namespace,d.defaclobjtype,ARRAY(SELECT x::text FROM unnest(d.defaclacl) x ORDER BY x::text) defaclacl FROM pg_default_acl d LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace) r)
)::text"""


def require(value, message):
    if not value:
        raise RuntimeError(message)


def catalog_equivalent(source, target):
    """Allow reviewed installed-extension upgrades, never application/security drift."""
    left, right = dict(source), dict(target)
    old_extensions = {e['extname']: e for e in left.pop('extensions') or []}
    new_extensions = {e['extname']: e for e in right.pop('extensions') or []}
    if old_extensions.keys() != new_extensions.keys():
        return False
    upgraded = set()
    for name, old in old_extensions.items():
        new = new_extensions[name]
        if {k: v for k, v in old.items() if k != 'extversion'} != {k: v for k, v in new.items() if k != 'extversion'}:
            return False
        if old['extversion'] != new['extversion']:
            upgraded.add(name)
    old_functions = {f['signature']: f for f in left.pop('functions') or []}
    new_functions = {f['signature']: f for f in right.pop('functions') or []}
    if not old_functions.keys() <= new_functions.keys():
        return False
    if any(new_functions[name] != old for name, old in old_functions.items()):
        return False
    for name in new_functions.keys() - old_functions.keys():
        item = new_functions[name]
        extension = item.get('extension')
        if (extension not in upgraded or item['owner'] != new_extensions[extension]['owner']
                or item['prosecdef'] or item['proconfig'] or item['proacl'] != item['default_acl']):
            return False
    return left == right


def restore_globals(text, initializer='billing_admin'):
    require(re.fullmatch(r'[a-z_][a-z0-9_]*', initializer), 'Unsupported source initializer identifier.')
    statement = f'CREATE ROLE {initializer};'
    require(text.splitlines().count(statement) == 1, 'Expected one source initializer declaration.')
    # Apply the initializer's final restrictions last, from the restored admin,
    # so a NOLOGIN/NOSUPERUSER source role cannot interrupt restoration.
    final = [line for line in text.splitlines() if line.startswith(f'ALTER ROLE {initializer} ')]
    require(final, 'Source initializer attributes are missing.')
    lines = [line for line in text.splitlines() if line != statement and line not in final]
    return '\n'.join(lines + ['SET ROLE billing_admin;'] + final + ['RESET ROLE;']) + '\n'


def catalog_hash(snapshot):
    return hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()


def env_patch(text, changes):
    """Replace only explicit keys; preserve unrelated settings and secret file paths."""
    result = []
    for line in text.splitlines():
        key = line.split('=', 1)[0].strip()
        if key not in changes:
            result.append(line)
    result.extend(f'{key}={value}' for key, value in changes.items())
    return '\n'.join(result) + '\n'


def atomic(path, content):
    temporary = path.with_suffix(path.suffix + '.new')
    temporary.write_text(content)
    temporary.chmod(0o600)
    temporary.replace(path)


class Maintenance:
    def __init__(self, args):
        self.args = args
        self.root = Path(args.root).resolve()
        self.marker = self.root / '.deployment/postgres-maintenance.json'
        self.work = self.root / '.deployment/postgres18-maintenance'
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.work.chmod(0o700)
        self.state = json.loads(self.marker.read_text()) if self.marker.exists() else {}
        os.chdir(self.root)

    def command(self, args, data=None, output=None, password=False):
        environment = dict(os.environ)
        if password:
            environment['PGPASSWORD'] = (self.root / '.deployment/secrets/admin_db_password').read_text().strip()
        result = subprocess.run(args, input=data, stdout=output or subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
        if result.returncode:
            (self.work / 'last-error.txt').write_bytes(result.stderr)
            raise RuntimeError('Maintenance command failed; private diagnostics in postgres18-maintenance/last-error.txt. Writers remain stopped after freeze.')
        return result.stdout or b''

    def save(self, **changes):
        self.state.update(changes)
        atomic(self.marker, json.dumps(self.state, indent=2) + '\n')

    def db(self):
        value = self.command(['docker', 'compose', 'ps', '-q', 'db']).decode().strip()
        require(value, 'Database container is not running.')
        return value

    def sql(self, container, query, user='billing_admin', database='billing'):
        return self.command(['docker', 'exec', '-i', *(['-e', 'PGPASSWORD'] if user == 'billing_admin' else []), container, 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1', '-U', user, '-d', database], query.encode(), password=user == 'billing_admin').decode().strip()

    def fingerprint(self, container, user='billing_admin'):
        raw = self.sql(container, CATALOG, user)
        return json.loads(raw)

    def counts(self, container, user='billing_admin'):
        names = json.loads(self.sql(container, "SELECT coalesce(json_agg(tablename ORDER BY tablename),'[]') FROM pg_tables WHERE schemaname='public'", user))
        result = {name: int(self.sql(container, 'SELECT count(*) FROM public."' + name.replace('"', '""') + '"', user)) for name in names}
        sequences = json.loads(self.sql(container, "SELECT coalesce(json_agg(sequencename ORDER BY sequencename),'[]') FROM pg_sequences WHERE schemaname='public'", user))
        for name in sequences:
            result['sequence:' + name] = self.sql(container, 'SELECT last_value::text || chr(58) || is_called::text FROM public."' + name.replace('"', '""') + '"', user)
        return result

    def preflight(self):
        require(re.fullmatch(r'(?:[a-zA-Z0-9_./:-]+@)?sha256:[a-f0-9]{64}', self.args.image), 'Supply a locally available digest-pinned PostgreSQL 18 image.')
        require(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{5,100}', self.args.volume), 'Invalid target volume name.')
        source = self.db()
        require(self.sql(source, 'SHOW server_version_num').startswith('17'), 'Source must be PostgreSQL 17.')
        info = json.loads(self.command(['docker', 'inspect', source]))[0]
        volumes = [m for m in info['Mounts'] if m['Destination'] == '/var/lib/postgresql/data']
        require(len(volumes) == 1 and volumes[0]['Type'] == 'volume', 'Expected named original PG17 volume.')
        original = volumes[0]['Name']
        require(self.args.volume != original and self.args.volume + '-rehearsal' != original, 'Target must never be original PG17 volume.')
        require(self.sql(source, "SELECT count(*) FROM pg_roles WHERE rolname='billing_migration_bootstrap'") == '0', 'Reserved migration bootstrap role exists on source.')
        require(self.sql(source, "SELECT count(*) FROM pg_tablespace WHERE spcname NOT IN ('pg_default','pg_global')") == '0', 'Custom tablespaces require a separate migration plan.')
        require(self.sql(source, "SELECT count(*) FROM pg_database WHERE datallowconn AND datname NOT IN ('billing','postgres','template1')") == '0', 'Unexpected additional database; refusing partial cluster migration.')
        self.command(['docker', 'image', 'inspect', self.args.image])
        version = self.command(['docker', 'run', '--rm', '--network', 'none', '--entrypoint', 'postgres', self.args.image, '--version']).decode()
        require('PostgreSQL) 18.' in version, 'Target image must run PostgreSQL 18.')
        size = int(self.sql(source, "SELECT pg_database_size('billing')"))
        docker_root = self.command(['docker', 'info', '--format', '{{.DockerRootDir}}']).decode().strip()
        required = max(1024 ** 3, size * 8)
        require(shutil.disk_usage(docker_root).free >= required and shutil.disk_usage(self.work).free >= required, 'Insufficient disk: require max(1GiB,8xDB size) free for two new volumes, dumps and rollback.')
        config = json.loads(self.command(['docker', 'compose', 'config', '--format', 'json']))
        available = int(next(line.split()[1] for line in Path('/proc/meminfo').read_text().splitlines() if line.startswith('MemAvailable:'))) * 1024
        require(available >= 384 * 1024 ** 2, 'Require at least384MiB available memory before running isolated restore.')
        require(config['services']['db']['environment'].get('PGDATA') == '/var/lib/postgresql/data', 'Reviewed compose must explicitly set stable PGDATA first.')
        return source, original, info['Image']

    def backup(self, source, prefix):
        for filename, command in [('globals.sql', ['pg_dumpall', '-U', 'billing_admin', '--globals-only']), ('billing.dump', ['pg_dump', '-U', 'billing_admin', '-d', 'billing', '-Fc'])]:
            path = self.work / (prefix + '-' + filename)
            with path.open('xb') as stream:
                self.command(['docker', 'exec', '-e', 'PGPASSWORD', source, *command], output=stream, password=True)
            require(path.stat().st_size > 0, 'Empty backup.')
            atomic(path.with_suffix(path.suffix + '.sha256'), hashlib.sha256(path.read_bytes()).hexdigest() + '\n')
        if prefix == 'final':
            bucket = (self.root / '.deployment/backup-bucket').read_text().strip()
            require(re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket), 'Invalid approved backup bucket.')
            backup_prefix = 'backups/postgres18/' + str(int(time.time())) + '/'
            for path in sorted(self.work.glob('final-*')):
                self.command(['aws', 's3', 'cp', str(path), 's3://' + bucket + '/' + backup_prefix + path.name, '--sse', 'AES256', '--only-show-errors'])
            self.save(backup_prefix=backup_prefix)

    def restore(self, volume, prefix):
        existing = self.command(['docker', 'volume', 'ls', '--format', '{{.Name}}']).decode().splitlines()
        require(volume not in existing, 'New target volume already exists: never overwrite or reuse a volume.')
        self.command(['docker', 'volume', 'create', '--label', 'cloud-billing.pg18-maintenance=true', volume])
        source_catalog = json.loads((self.work / (prefix + '-source-catalog.json')).read_text())
        initializer = next(e['owner'] for e in source_catalog['extensions'] if e['extname'] == 'plpgsql')
        require(re.fullmatch(r'[a-z_][a-z0-9_]*', initializer), 'Unsupported source initializer identifier.')
        password = self.work / (prefix + '-bootstrap-password')
        atomic(password, secrets.token_urlsafe(40))
        # Private parent directory prevents host access; postgres UID must read its mounted secret.
        password.chmod(0o444)
        container = self.command(['docker', 'run', '-d', '--network', 'none', '--memory', '512m', '--cpus', '1',
            '-e', 'PGDATA=/var/lib/postgresql/data', '-e', 'POSTGRES_USER=' + initializer, '-e', 'POSTGRES_DB=postgres',
            '-e', 'POSTGRES_PASSWORD_FILE=/run/bootstrap-password', '-v', str(password) + ':/run/bootstrap-password:ro',
            '-v', volume + ':/var/lib/postgresql/data', self.args.image,
            'postgres', '-c', 'shared_buffers=64MB', '-c', 'max_connections=30']).decode().strip()
        try:
            for _ in range(60):
                ready = subprocess.run(['docker', 'exec', container, 'pg_isready', '-U', 'billing_admin'], capture_output=True)
                if ready.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError('Isolated PG18 did not become ready.')
            self.sql(container, restore_globals((self.work / (prefix + '-globals.sql')).read_text(), initializer), initializer, 'postgres')
            original_initializer = next(r for r in source_catalog['roles'] if r['rolname'] == initializer)
            if original_initializer['rolpassword'] is None:
                # pg_dumpall omits a password clause for NULL; clear the temporary
                # initialization password explicitly instead of creating access.
                self.sql(container, f'ALTER ROLE {initializer} PASSWORD NULL', 'billing_admin', 'postgres')
            self.command(['docker', 'exec', '-i', '-e', 'PGPASSWORD', container, 'pg_restore', '-U', 'billing_admin', '-d', 'postgres', '--create', '--exit-on-error'],
                         (self.work / (prefix + '-billing.dump')).read_bytes(), password=True)
            self.sql(container, 'ANALYZE')
            digest = self.fingerprint(container)
            counts = self.counts(container)
            # No temporary login is created; source administrator credentials and
            # attributes replace the initialization secret via the globals dump.
            atomic(self.work / (prefix + '-target-catalog.json'), json.dumps(digest, sort_keys=True))
            return digest, counts
        finally:
            self.command(['docker', 'stop', container])
            self.command(['docker', 'rm', container])
            password.unlink(missing_ok=True)

    def rehearse(self):
        require(not self.state, 'Maintenance already has a receipt; inspect it instead of restarting.')
        source, original, old_image = self.preflight()
        self.backup(source, 'rehearsal')
        before = self.fingerprint(source)
        atomic(self.work / 'rehearsal-source-catalog.json', json.dumps(before, sort_keys=True))
        digest, counts = self.restore(self.args.volume + '-rehearsal', 'rehearsal')
        require(catalog_equivalent(before, digest), 'Rehearsal role/grant/RLS catalog differs; source unchanged.')
        self.save(phase='rehearsed', original_volume=original, original_image=old_image,
                  target_volume=self.args.volume, target_image=self.args.image, rehearsal_counts=counts, source_extensions=before['extensions'], target_extensions=digest['extensions'])
        print('Online isolated restore rehearsal passed. Original volume and writers unchanged.')

    def freeze(self):
        self.save(phase='frozen')
        self.command(['systemctl', 'stop', 'cloud-billing-collector'])
        self.command(['docker', 'compose', 'stop', 'app'])
        source = self.db()
        require(self.sql(source, "SELECT count(*) FROM pg_stat_activity WHERE datname='billing' AND pid<>pg_backend_pid() AND backend_type='client backend'") == '0', 'Other billing sessions remain. Keep writers stopped and investigate; do not terminate unknown sessions automatically.')
        return source

    def cutover(self):
        require(self.state.get('phase') == 'rehearsed', 'Successful rehearsal required.')
        require(self.args.image == self.state['target_image'] and self.args.volume == self.state['target_volume'], 'Use the rehearsed image and target volume.')
        source, original, _ = self.preflight()
        require(original == self.state['original_volume'], 'Source volume changed after rehearsal.')
        source = self.freeze()
        before, counts = self.fingerprint(source), self.counts(source)
        atomic(self.work / 'final-source-catalog.json', json.dumps(before, sort_keys=True))
        self.backup(source, 'final')
        digest, restored_counts = self.restore(self.args.volume, 'final')
        require(catalog_equivalent(before, digest) and counts == restored_counts, 'Final restored roles/grants/RLS or table counts differ. Writers remain stopped.')
        env = self.root / '.env'
        require(env.exists(), 'Expected existing .env.')
        original_env = self.work / 'original.env'
        require(not original_env.exists(), 'Original environment backup already exists.')
        atomic(original_env, env.read_text())
        self.save(phase='switching', catalog_hash=catalog_hash(before), final_counts=counts)
        self.command(['docker', 'compose', 'stop', 'db'])
        atomic(env, env_patch(env.read_text(), {'BILLING_POSTGRES_IMAGE': self.args.image, 'BILLING_POSTGRES_VOLUME': self.args.volume}))
        config = json.loads(self.command(['docker', 'compose', 'config', '--format', 'json']))
        require(config['services']['db']['image'] == self.args.image and config['volumes']['pgdata']['name'] == self.args.volume, 'Compose did not resolve reviewed image/volume variables.')
        self.command(['docker', 'compose', 'up', '-d', '--no-build', '--no-deps', 'db'])
        for _ in range(60):
            ready = subprocess.run(['docker', 'exec', self.db(), 'pg_isready', '-U', 'billing_admin', '-d', 'billing'], capture_output=True)
            if ready.returncode == 0:
                break
            time.sleep(1)
        target = self.db()
        require(self.sql(target, 'SHOW server_version_num').startswith('18'), 'Cutover is not PostgreSQL18.')
        require(catalog_equivalent(before, self.fingerprint(target)) and self.counts(target) == counts, 'Cutover verification failed.')
        self.save(phase='awaiting-verification')
        print('PG18 cutover verified; writers remain stopped. Verify web/collector runtime identities, TLS and RLS before reopen. Rollback remains available.')

    def rollback(self):
        require(self.state.get('phase') in ('frozen', 'switching', 'awaiting-verification'), 'Rollback allowed only before writers reopen.')
        self.command(['systemctl', 'stop', 'cloud-billing-collector'])
        self.command(['docker', 'compose', 'stop', 'app'])
        backup = self.work / 'original.env'
        if backup.exists():
            self.command(['docker', 'compose', 'stop', 'db'])
            atomic(self.root / '.env', env_patch(backup.read_text(), {'BILLING_POSTGRES_IMAGE': self.state['original_image'], 'BILLING_POSTGRES_VOLUME': self.state['original_volume']}))
            self.command(['docker', 'compose', 'up', '-d', '--no-build', '--no-deps', 'db'])
        self.save(phase='rolled-back')
        print('Original configuration restored; writers remain stopped. Verify PG17 then explicitly reopen with reviewed runtime checks.')

    def reopen(self):
        require(self.state.get('phase') in ('awaiting-verification', 'rolled-back'), 'Database must be verified or rolled back first.')
        require(self.args.runtime_checks_passed, 'Require --runtime-checks-passed after both identity/RLS/TLS checks and compatible runtime/schema checks.')
        # Record boundary before any writer can start: rollback is forbidden from here.
        self.save(phase='writers-reopened', reopened_at=time.time())
        self.command(['docker', 'compose', 'up', '-d', '--no-build', '--no-deps', 'app'])
        self.command(['systemctl', 'start', 'cloud-billing-collector'])
        print('Writers reopened. Automatic PG17 rollback is now forbidden; new writes require a separate recovery plan.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['preflight', 'rehearse', 'cutover', 'rollback', 'reopen'])
    parser.add_argument('--root', default='/opt/cloud-billing')
    parser.add_argument('--image', required=True)
    parser.add_argument('--volume', required=True)
    parser.add_argument('--runtime-checks-passed', action='store_true')
    args = parser.parse_args()
    os.umask(0o077)
    require(os.geteuid() == 0, 'Run through reviewed root maintenance access.')
    with open('/run/lock/cloud-billing-release.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        runner = Maintenance(args)
        outcome = getattr(runner, args.action)()
        if args.action == 'preflight':
            print('Preflight passed: PG17 source, distinct new PG18 volume, pinned local image, stable PGDATA, disk and memory gates verified.')


if __name__ == '__main__':
    main()
