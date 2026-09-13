"""Start only this worktree's isolated loopback PostgreSQL fixture cluster."""
import os
import secrets
import shlex
import subprocess
from pathlib import Path
root=Path(__file__).resolve().parents[1]
private=root/'.deployment';private.mkdir(exist_ok=True,mode=0o700)
bin_dir=Path('/opt/homebrew/opt/postgresql@17/bin')
data=private/'test-pgdata';password_file=private/'test-pg-password'
if not password_file.exists():
 password_file.write_text(secrets.token_urlsafe(32));password_file.chmod(0o600)
password=password_file.read_text()
if not (data/'PG_VERSION').exists():
 subprocess.run([str(bin_dir/'initdb'),'-D',str(data),'-U','billing_test_admin','--auth=scram-sha-256','--pwfile',str(password_file),'--locale=C','-E','UTF8'],check=True,stdout=subprocess.DEVNULL)
status=subprocess.run([str(bin_dir/'pg_ctl'),'-D',str(data),'status'],stdout=subprocess.DEVNULL)
if status.returncode:
 subprocess.run([str(bin_dir/'pg_ctl'),'-D',str(data),'-l',str(private/'postgres-test.log'),'-o',f'-h 127.0.0.1 -p 55432 -k {private}','start'],check=True)
import psycopg
from psycopg import sql
with psycopg.connect(host='127.0.0.1',port=55432,user='billing_test_admin',password=password,dbname='postgres',autocommit=True) as c:
 for name in ['billing_security_checks','billing_security_scale','billing_security_restore']:
  if not c.execute('SELECT 1 FROM pg_database WHERE datname=%s',[name]).fetchone():c.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
values={'DEBUG':'true','DB_HOST':'127.0.0.1','DB_PORT':'55432','DB_USER':'billing_test_admin','DB_PASSWORD':password,'DB_NAME':'billing_security_checks','AWS_EC2_METADATA_DISABLED':'true'}
env_file=private/'test.env';env_file.write_text('\n'.join(f'export {key}={shlex.quote(value)}' for key,value in values.items())+'\n');env_file.chmod(0o600)
print('Isolated PostgreSQL ready on 127.0.0.1:55432. Credentials are in ignored .deployment/test.env.')
