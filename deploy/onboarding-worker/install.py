#!/usr/bin/env python3
"""Root-only one-time setup after reviewed migrations and broker provisioning.

Does not grant AWS rights or change PostgreSQL privileges. Copies the existing
exact allowlist, preserves credentials on disk, and admits only the native host
address to the existing TLS/password-protected administration DB role.
"""
import argparse
import grp
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile


def atomic_text(path, content, mode, uid=0, gid=0):
    path = Path(path)
    if path.is_symlink():
        raise ValueError('Refusing symlink: ' + str(path))
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--broker-arn', required=True)
    args = p.parse_args()
    if os.geteuid() != 0:
        raise ValueError('Run with separately authorized root administration')
    root = Path('/opt/cloud-billing')
    collector_path = Path('/etc/cloud-billing/collector.env')
    env = {}
    for line in collector_path.read_text().splitlines():
        if line.strip() and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            env[key] = shlex.split(value)[0] if value else ''
    broker = re.fullmatch(r'arn:aws:lambda:([a-z0-9-]+):([0-9]{12}):function:CloudBillingOnboardingBroker', args.broker_arn)
    collector = re.fullmatch(r'arn:aws:iam::([0-9]{12}):role/CloudBillingCollector', env.get('COLLECTOR_ROLE_ARN', ''))
    if not broker or not collector or broker[2] != collector[1] or broker[1] != env.get('AWS_REGION'):
        raise ValueError('Broker must match the configured collector account and region')
    if env.get('DB_SSLMODE') != 'verify-full' or env.get('DB_USER') != 'billing_collector':
        raise ValueError('Expected the existing verified collector DB configuration')
    host = ipaddress.ip_address(env['DB_HOST'])
    if not host.is_private or host.version != 4:
        raise ValueError('Expected this native host private IPv4 address')
    secret = root / '.deployment/secrets/admin_db_password'
    if not secret.is_file() or secret.stat().st_uid != 0 or secret.stat().st_mode & 0o077:
        raise ValueError('Administration credential must remain root-only')
    backup = root / '.deployment/onboarding-bootstrap-backup'
    backup.mkdir(mode=0o700, exist_ok=True)
    for source in (collector_path, root / '.deployment/pg_hba.conf'):
        target = backup / source.name
        if not target.exists():
            shutil.copy2(source, target)
            target.chmod(0o600)
    roles = json.loads(Path(env['COLLECTOR_ALLOWLIST_FILE']).read_text())
    if not isinstance(roles, list) or any(not isinstance(v, str) or not v.startswith('arn:aws:iam::') or '*' in v or '?' in v for v in roles):
        raise ValueError('Invalid existing exact-role allowlist')
    group = grp.getgrnam('billing-collector').gr_gid
    state = Path('/var/lib/cloud-billing-onboarding')
    state.mkdir(mode=0o750, exist_ok=True)
    os.chown(state, 0, group)
    state.chmod(0o750)
    target = state / 'approved-roles.json'
    if target.exists():
        roles = sorted(set(roles) | set(json.loads(target.read_text())))
    atomic_text(target, json.dumps(roles, indent=2) + '\n', 0o640, gid=group)
    env['COLLECTOR_ALLOWLIST_FILE'] = str(target)
    # Preserve every existing collector setting; credentials are never printed.
    atomic_text(collector_path, ''.join(f'{k}={shlex.quote(v)}\n' for k, v in env.items()), 0o600)
    env.update(RUNTIME_ROLE='admin', DB_USER='billing_admin', DB_PASSWORD_FILE=str(secret),
               AWS_EC2_METADATA_DISABLED='false', ONBOARDING_BROKER_FUNCTION=args.broker_arn,
               ONBOARDING_LOCK_FILE='/run/cloud-billing-activation/lock')
    env.pop('DB_PASSWORD', None)
    env.pop('DATABASE_URL', None)
    atomic_text('/etc/cloud-billing/activation.env', ''.join(f'{k}={shlex.quote(v)}\n' for k, v in env.items()), 0o600)
    hba = root / '.deployment/pg_hba.conf'
    rule = f'hostssl billing billing_admin {host}/32 scram-sha-256'
    text = hba.read_text()
    if rule not in text.splitlines():
        text = rule + '\n' + text
        # The file is a Docker bind mount; update in place to retain the inode.
        with hba.open('w') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        subprocess.run(['docker', 'compose', 'exec', '-T', 'db', 'sh', '-c', 'kill -HUP 1'], cwd=root, check=True)
    unit = Path('/etc/systemd/system/cloud-billing-activation.service')
    atomic_text(unit, (root / 'deploy/onboarding-worker/activation.service').read_text(), 0o644)
    subprocess.run(['systemctl', 'daemon-reload'], check=True)
    print('Activation configuration installed. Validate the admin runtime, then enable cloud-billing-activation and restart the collector.')


if __name__ == '__main__':
    main()
