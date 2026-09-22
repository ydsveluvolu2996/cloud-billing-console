#!/usr/bin/env python3
"""One-time hosting-account setup. Emits a reviewable plan by default; --apply installs it."""
import argparse
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import boto3
from botocore.exceptions import ClientError

from release import ACCOUNT, REGION, BUCKET, DOCUMENT, INSTANCES, REPOSITORY_ID

HERE = Path(__file__).resolve().parent
ROLE = 'CloudBillingGitHubRelease'
ENVIRONMENT = 'billing-production'
PROVIDER = 'arn:aws:iam::' + ACCOUNT + ':oidc-provider/token.actions.githubusercontent.com'
SUBJECT = 'repo:ydsveluvolu2996@229068958/cloud-billing-console@1361643722:environment:' + ENVIRONMENT


def policies(subject=SUBJECT):
    if subject != SUBJECT:
        raise ValueError('OIDC subject differs from the reviewed immutable repository/environment identity')
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'Federated': PROVIDER},
             'Action': 'sts:AssumeRoleWithWebIdentity', 'Condition': {'StringEquals': {
                 'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
                 'token.actions.githubusercontent.com:sub': subject}}}]}
    policy = {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'UploadEncryptedReleases', 'Effect': 'Allow', 'Action': 's3:PutObject',
         'Resource': 'arn:aws:s3:::' + BUCKET + '/releases/github/*',
         'Condition': {'StringEquals': {'s3:x-amz-server-side-encryption': 'AES256'}}},
        {'Sid': 'OnlyFixedReleaseCommand', 'Effect': 'Allow', 'Action': 'ssm:SendCommand',
         'Resource': ['arn:aws:ssm:' + REGION + ':' + ACCOUNT + ':document/' + DOCUMENT] +
                     ['arn:aws:ec2:' + REGION + ':' + ACCOUNT + ':instance/' + i for i in INSTANCES.values()]},
        {'Sid': 'ReadCommandReceipt', 'Effect': 'Allow', 'Action': 'ssm:GetCommandInvocation', 'Resource': '*'}]}
    reader = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': 's3:GetObjectVersion',
              'Resource': 'arn:aws:s3:::' + BUCKET + '/releases/github/*'}]}
    return trust, policy, reader


def document():
    parameters = {
        'action': {'type': 'String', 'allowedValues': ['stage', 'activate', 'rollback', 'status']},
        'releaseId': {'type': 'String', 'allowedPattern': '^[a-f0-9]{40}-[0-9]+-[0-9]+$'},
        # SSM's RE2 parser caps an individual counted repetition at 1000.
        'manifestVersion': {'type': 'String', 'allowedPattern': '^[A-Za-z0-9._+/=-]{1,512}[A-Za-z0-9._+/=-]{0,512}$'},
        'manifestSha256': {'type': 'String', 'allowedPattern': '^[a-f0-9]{64}$'}}
    for value in parameters.values():
        value['interpolationType'] = 'ENV_VAR'
    return {'schemaVersion': '2.2', 'description': 'Fixed billing release executor; accepts immutable artifact coordinates only.',
            'parameters': parameters, 'mainSteps': [{'action': 'aws:runShellScript', 'name': 'release', 'inputs': {
                'timeoutSeconds': '900', 'runCommand': ['set -eu',
                'exec /usr/bin/python3 /usr/local/lib/cloud-billing-release/agent.py "$SSM_action" "$SSM_releaseId" "$SSM_manifestVersion" "$SSM_manifestSha256"']}}]}


def compatibility():
    root = HERE.parent.parent
    files = sorted(str(p.relative_to(root)) for p in (root / 'billing/migrations').glob('*.py'))
    files += ['deploy/database-roles.sql', 'deploy/user-administration.sql', 'deploy/activation-requests.sql', 'deploy/onboarding-worker/activation.service', 'deploy/onboarding-worker/install.py', 'compose.yaml', 'deploy/collector.service'] + ['deploy/single-ec2/metadata_guard.py', 'deploy/single-ec2/metadata-guard.service', 'deploy/single-ec2/docker-metadata.conf', 'deploy/single-ec2/collector.conf']
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}


def host_config(runtime):
    return {'root': '/opt/cloud-billing', 'releases': '/opt/cloud-billing-releases', 'runtime': runtime,
            'region': REGION, 'bucket': BUCKET, 'account_id': ACCOUNT, 'repository_id': REPOSITORY_ID,
            'instance_id': INSTANCES[runtime], 'hostname': 'yscloudbilling.13.207.30.208.sslip.io',
            'compatibility': compatibility()}


def remote(ssm, instance, script):
    payload = base64.b64encode(script.encode()).decode()
    # Static bootstrap script encoded as data; this administrative path is not granted to the GitHub role.
    command = "printf '%s' '" + payload + "' | base64 -d | /usr/bin/python3"
    response = ssm.send_command(InstanceIds=[instance], DocumentName='AWS-RunShellScript',
                               Parameters={'commands': [command], 'executionTimeout': ['300']}, TimeoutSeconds=120,
                               Comment='One-time billing GitHub deployment bootstrap')
    command_id = response['Command']['CommandId']
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        try:
            result = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance)
        except ClientError as error:
            if error.response['Error']['Code'] != 'InvocationDoesNotExist':
                raise
            time.sleep(3)
            continue
        if result['Status'] == 'Success':
            return json.loads(result['StandardOutputContent'])
        if result['Status'] not in ('Pending', 'InProgress', 'Delayed'):
            raise RuntimeError('Bootstrap preflight/install failed; inspect SSM command ' + command_id)
        time.sleep(3)
    raise TimeoutError('Bootstrap command pending: ' + command_id)


def host_script(config, install=False):
    agent_bytes = (HERE / 'agent.py').read_bytes()
    additive_bytes = (HERE / 'additive_migrations.py').read_bytes()
    additive_hash = hashlib.sha256(additive_bytes).hexdigest()
    if ("ADDITIVE_MODULE_SHA256 = '" + additive_hash + "'").encode() not in agent_bytes:
        raise ValueError('The additive helper must match the executor checksum pin')
    agent = base64.b64encode(agent_bytes).decode()
    additive = base64.b64encode(additive_bytes).decode()
    return '''import base64,fcntl,hashlib,json,os,shutil,subprocess
from pathlib import Path
os.umask(0o077)
config = CONFIG
root = Path(config['root'])
assert os.geteuid() == 0 and root.is_dir()
cli=shutil.which('aws');assert cli, 'AWS CLI must be installed once on this host'
config['aws_cli']=cli
assert json.loads(subprocess.check_output([cli,'sts','get-caller-identity','--region',config['region']]))['Account']==config['account_id']
assert shutil.disk_usage(root).free > 5_000_000_000, 'At least 5 GB free disk is required'
assert subprocess.check_output(['/usr/bin/python3','-c','import sys;print(str(sys.version_info.major)+"."+str(sys.version_info.minor))'],text=True).strip()=='3.12'
for name,expected in config['compatibility'].items():
 if name.startswith('billing/migrations/'):
  assert hashlib.sha256((root/name).read_bytes()).hexdigest()==expected, 'Migration baseline differs'
assert sorted(p.name for p in (root/'billing/migrations').glob('*.py'))==sorted(Path(n).name for n in config['compatibility'] if n.startswith('billing/migrations/'))
if config['runtime'] in ('web','combined'):
 assert (root/'.deployment/database-policy-sha').read_text().strip()=='5461f736b58aad33587e91dce237a627ea1a31f2', 'Review active database policy first'
 subprocess.run(['docker','compose','exec','-T','app','python','manage.py','verify_runtime'],cwd=root,check=True,stdout=subprocess.DEVNULL)
 assert (root/'deploy/backup.sh').is_file()
if config['runtime'] in ('collector','combined'):
 subprocess.run(['systemctl','is-active','--quiet','cloud-billing-collector'],check=True)
 subprocess.run(['/usr/bin/python3','-c','import venv,ensurepip'],check=True)
 subprocess.run(['systemd-run','--quiet','--wait','--pipe','--collect','--uid=billing-collector','--working-directory='+str(root),'--property=EnvironmentFile=/etc/cloud-billing/collector.env',str(root/'.venv/bin/python'),'manage.py','verify_runtime'],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
if config['runtime']=='combined':
 subprocess.run(['/usr/local/sbin/cloud-billing-metadata-guard','--check'],check=True,stdout=subprocess.DEVNULL)
if INSTALL:
 lock=os.open('/run/lock/cloud-billing-release.lock',os.O_CREAT|os.O_WRONLY|os.O_NOFOLLOW,0o600)
 fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
 try:
  # Another release may have finished after the initial read-only preflight.
  for name,expected in config['compatibility'].items():
   assert hashlib.sha256((root/name).read_bytes()).hexdigest()==expected, 'Protected baseline changed before installation'
  assert sorted(p.name for p in (root/'billing/migrations').glob('*.py'))==sorted(Path(n).name for n in config['compatibility'] if n.startswith('billing/migrations/'))
  def sync_dir(path):
   descriptor=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
   try:os.fsync(descriptor)
   finally:os.close(descriptor)
  def replace_file(path,data,mode):
   temporary=path.with_suffix('.install-new');created=False
   try:
    with temporary.open('xb') as stream:
     created=True;stream.write(data);stream.flush();os.fsync(stream.fileno())
    temporary.chmod(mode);temporary.replace(path);sync_dir(path.parent)
   finally:
    if created:temporary.unlink(missing_ok=True)
  target=Path('/usr/local/lib/cloud-billing-release');target.mkdir(parents=True,exist_ok=True)
  folder=Path('/etc/cloud-billing');folder.mkdir(parents=True,exist_ok=True)
  for parent in (target,folder):
   for item in (parent,*parent.parents):
    st=item.lstat();assert not item.is_symlink() and st.st_uid==0 and not st.st_mode&0o022, 'Unprotected installer path'
  additions={target/'additive_migrations.py':(base64.b64decode(ADDITIVE),0o644),target/'agent.py':(base64.b64decode(AGENT),0o755),folder/'release.json':((json.dumps(config,indent=2)+'\\n').encode(),0o600)}
  previous={}
  for path,(data,mode) in additions.items():
   assert not path.is_symlink(), 'Linked installer target'
   if path.exists():
    st=path.stat();assert st.st_uid==0 and not st.st_mode&0o022, 'Unprotected installer target'
   previous[path]=(path.read_bytes(),path.stat().st_mode&0o777) if path.exists() else None
   if path.suffix=='.py':compile(data,str(path),'exec')
   assert not path.with_suffix('.install-new').exists() and not path.with_suffix('.install-new').is_symlink(), 'Inspect an interrupted installer first'
  try:
   for path,(data,mode) in additions.items():
    replace_file(path,data,mode)
   Path(config['releases']).mkdir(mode=0o755,exist_ok=True)
   (root/'.deployment').mkdir(mode=0o700,exist_ok=True)
  except BaseException:
   for path,old in previous.items():
    if old is None:path.unlink(missing_ok=True);sync_dir(path.parent)
    else:replace_file(path,*old)
   raise
 finally:os.close(lock)
print(json.dumps({'instance':config['instance_id'],'runtime':config['runtime'],'status':'installed' if INSTALL else 'preflight_ok','aws_cli':cli}))
'''.replace('CONFIG', repr(config)).replace('INSTALL', repr(install)).replace('ADDITIVE', repr(additive)).replace('AGENT', repr(agent))


def apply(profile, subject):
    session = boto3.Session(profile_name=profile, region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise ValueError('Sign in to hosting account ' + ACCOUNT + ', not a customer account')
    iam, ssm, s3, ec2 = (session.client(name) for name in ('iam', 'ssm', 's3', 'ec2'))
    if s3.get_bucket_versioning(Bucket=BUCKET, ExpectedBucketOwner=ACCOUNT).get('Status') != 'Enabled':
        raise ValueError('Existing artifact bucket must have versioning enabled')
    roles = {}
    for runtime, instance in INSTANCES.items():
        info = ssm.describe_instance_information(Filters=[{'Key': 'InstanceIds', 'Values': [instance]}])['InstanceInformationList']
        if not info or info[0]['PingStatus'] != 'Online' or tuple(int(x) for x in info[0]['AgentVersion'].split('.')) < (3, 3, 2746, 0):
            raise ValueError('An online SSM agent >=3.3.2746.0 is required on ' + instance)
        result = ec2.describe_instances(InstanceIds=[instance])['Reservations'][0]['Instances'][0]
        profile_name = result['IamInstanceProfile']['Arn'].rsplit('/', 1)[1]
        roles[runtime] = iam.get_instance_profile(InstanceProfileName=profile_name)['InstanceProfile']['Roles'][0]['RoleName']
        print(json.dumps(remote(ssm, instance, host_script(host_config(runtime)))))
    trust, policy, reader = policies(subject)
    try:
        provider = iam.get_open_id_connect_provider(OpenIDConnectProviderArn=PROVIDER)
        if 'sts.amazonaws.com' not in provider['ClientIDList']:
            iam.add_client_id_to_open_id_connect_provider(OpenIDConnectProviderArn=PROVIDER, ClientID='sts.amazonaws.com')
    except iam.exceptions.NoSuchEntityException:
        iam.create_open_id_connect_provider(Url='https://token.actions.githubusercontent.com', ClientIDList=['sts.amazonaws.com'],
                                            Tags=[{'Key': 'Application', 'Value': 'CloudBilling'}])
    try:
        existing = iam.get_role(RoleName=ROLE)['Role']
        if not any(t['Key'] == 'Application' and t['Value'] == 'CloudBilling' for t in existing.get('Tags', [])):
            raise ValueError('Existing deployment role is not owned by this setup')
        iam.update_assume_role_policy(RoleName=ROLE, PolicyDocument=json.dumps(trust))
        iam.update_role(RoleName=ROLE, MaxSessionDuration=7200)
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(RoleName=ROLE, AssumeRolePolicyDocument=json.dumps(trust), MaxSessionDuration=7200,
                        Description='GitHub OIDC billing release through fixed SSM executor', Tags=[{'Key': 'Application', 'Value': 'CloudBilling'}])
    iam.put_role_policy(RoleName=ROLE, PolicyName='FixedBillingRelease', PolicyDocument=json.dumps(policy))
    try:
        existing_doc = ssm.get_document(Name=DOCUMENT, DocumentFormat='JSON')
        if json.loads(existing_doc['Content']) != document():
            raise ValueError('Existing SSM release document differs; review it before replacing')
    except ssm.exceptions.InvalidDocument:
        ssm.create_document(Name=DOCUMENT, DocumentType='Command', DocumentFormat='JSON', Content=json.dumps(document()),
                            Tags=[{'Key': 'Application', 'Value': 'CloudBilling'}])
    for runtime, instance in INSTANCES.items():
        iam.put_role_policy(RoleName=roles[runtime], PolicyName='ReadVersionedGitHubReleases', PolicyDocument=json.dumps(reader))
        print(json.dumps(remote(ssm, instance, host_script(host_config(runtime), install=True))))
    print(json.dumps({'role_arn': 'arn:aws:iam::' + ACCOUNT + ':role/' + ROLE, 'document': DOCUMENT, 'status': 'bootstrap_complete'}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', default='cloud-billing')
    parser.add_argument('--subject', default=SUBJECT)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if args.apply:
        apply(args.profile, args.subject)
    else:
        trust, policy, reader = policies(args.subject)
        print(json.dumps({'account': ACCOUNT, 'instances': INSTANCES, 'trust': trust, 'release_policy': policy,
                          'host_read_policy': reader, 'ssm_document': document(),
                          'compatibility': compatibility(), 'agent_sha256': hashlib.sha256((HERE/'agent.py').read_bytes()).hexdigest()}, indent=2))
