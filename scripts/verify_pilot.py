"""Read-only AWS configuration checks; run only with authorized review credentials."""
import argparse,json,os
from pathlib import Path
from datetime import datetime,timezone
import boto3
from botocore.config import Config
p=argparse.ArgumentParser();p.add_argument('--stack',required=True);p.add_argument('--region',required=True)
p.add_argument('--output',required=True);a=p.parse_args()
session=boto3.Session(region_name=a.region);cfg=Config(connect_timeout=5,read_timeout=15,retries={'max_attempts':2})
def client(name):return session.client(name,config=cfg)
resources=client('cloudformation').describe_stack_resources(StackName=a.stack)['StackResources']
ids={r['LogicalResourceId']:r['PhysicalResourceId'] for r in resources};checks={}
ec2=client('ec2');iam=client('iam')
instances={}
for logical in ('Server','CollectorServer'):
 instance=ec2.describe_instances(InstanceIds=[ids[logical]])['Reservations'][0]['Instances'][0];instances[logical]=instance
 checks[logical+'_imds_v2']=instance['MetadataOptions']['HttpTokens']=='required' and instance['MetadataOptions']['HttpPutResponseHopLimit']==1
 volumes=ec2.describe_volumes(VolumeIds=[b['Ebs']['VolumeId'] for b in instance['BlockDeviceMappings']])['Volumes']
 checks[logical+'_encrypted_volumes']=all(v['Encrypted'] and v['AvailabilityZone'].startswith(a.region) for v in volumes)
 groups=ec2.describe_security_groups(GroupIds=[g['GroupId'] for g in instance['SecurityGroups']])['SecurityGroups']
 public=[]
 for group in groups:
  for rule in group['IpPermissions']:
   exposed=any(r['CidrIp']=='0.0.0.0/0' for r in rule.get('IpRanges',[])) or any(r['CidrIpv6']=='::/0' for r in rule.get('Ipv6Ranges',[]))
   if exposed:public.append((rule.get('FromPort',0),rule.get('ToPort',65535)))
 checks[logical+'_no_public_admin_db']=not any(lo<=port<=hi for lo,hi in public for port in (22,5432))
 checks[logical+'_restricted_https']=not any(lo<=443<=hi for lo,hi in public)
 checks[logical+'_no_public_collector_ingress']=not public if logical=='CollectorServer' else True
checks['distinct_instances_profiles']=instances['Server']['InstanceId']!=instances['CollectorServer']['InstanceId'] and instances['Server']['IamInstanceProfile']['Arn']!=instances['CollectorServer']['IamInstanceProfile']['Arn']
s3=client('s3');bucket=ids['Artifacts']
checks['backup_encryption']=bool(s3.get_bucket_encryption(Bucket=bucket)['ServerSideEncryptionConfiguration']['Rules'])
checks['backup_public_access_block']=all(s3.get_public_access_block(Bucket=bucket)['PublicAccessBlockConfiguration'].values())
checks['backup_versioning']=s3.get_bucket_versioning(Bucket=bucket).get('Status')=='Enabled'
policy=json.loads(s3.get_bucket_policy(Bucket=bucket)['Policy'])
checks['backup_tls_deny']=any(s.get('Effect')=='Deny' and str(s.get('Condition',{}).get('Bool',{}).get('aws:SecureTransport','')).lower()=='false' for s in policy['Statement'])
checks['storage_region']=s3.get_bucket_location(Bucket=bucket).get('LocationConstraint','us-east-1')==a.region
role=iam.get_role(RoleName=ids['WebRole'])['Role']['Arn']
result=iam.simulate_principal_policy(PolicySourceArn=role,ActionNames=['sts:AssumeRole','iam:PutRolePolicy'],ResourceArns=['*'])
checks['web_aws_privilege_denied']=all(r['EvalDecision']!='allowed' for r in result['EvaluationResults'])
logs=client('logs').describe_log_groups(logGroupNamePrefix=ids['AuditLogGroup'])['logGroups']
checks['audit_retention']=any(g['logGroupName']==ids['AuditLogGroup'] and g.get('retentionInDays',0)>=90 for g in logs)
report={'at':datetime.now(timezone.utc).isoformat(),'checks':checks,'passed':all(checks.values()),'pending':['Host metadata reachability from actual app container','Private DB TLS and actual login checks','SSM-only administration','Caddy HTTP/HTTPS/certificate renewal','Central audit arrival and deletion denial','Customer STS/trust verification','Backup restore from actual production backup']}
path=Path(a.output);path.write_text(json.dumps(report,indent=2)+'\n');path.chmod(0o600)
print('Read-only configuration checks passed' if report['passed'] else 'Configuration checks failed; inspect restricted evidence')
if not report['passed']:raise SystemExit(1)
