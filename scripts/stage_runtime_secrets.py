"""Stage only this host's Secrets Manager fields; never writes to Git or stdout."""
import argparse,json,os,tempfile
from pathlib import Path
import boto3
p=argparse.ArgumentParser();p.add_argument('--runtime',choices=['web','collector'],required=True)
p.add_argument('--secret-arn',required=True);p.add_argument('--region',required=True)
p.add_argument('--output',required=True);p.add_argument('--owner-uid',required=True,type=int);a=p.parse_args()
root=Path(a.output).resolve()
if '.deployment' not in root.parts and not str(root).startswith('/etc/cloud-billing/'):
 raise SystemExit('Use a restricted .deployment or /etc/cloud-billing directory')
root.mkdir(parents=True,exist_ok=True);root.chmod(0o700)
fields={'web':['django_secret','web_db_password'],'collector':['collector_db_password','collector_django_secret']}[a.runtime]
client=boto3.client('secretsmanager',region_name=a.region)
secret=json.loads(client.get_secret_value(SecretId=a.secret_arn)['SecretString'])
for name in fields:
 value=secret.get(name)
 if not isinstance(value,str) or len(value)<20:raise SystemExit('Required runtime secret missing or too short')
for name in fields:
 fd,path=tempfile.mkstemp(dir=root,prefix='.staging-')
 try:
  with os.fdopen(fd,'w') as f:f.write(secret[name])
  os.chmod(path,0o600);os.chown(path,a.owner_uid,-1);os.replace(path,root/name)
 finally:
  if os.path.exists(path):os.unlink(path)
print('Runtime-specific secrets staged. No secret values were logged.')
