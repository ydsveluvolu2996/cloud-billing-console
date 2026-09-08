from pathlib import Path
from urllib.parse import urlencode
import boto3
from botocore.config import Config
import yaml
from django.conf import settings


def customer_template(customer):
    if not settings.COLLECTOR_ROLE_ARN:
        raise ValueError('The collector IAM role is not configured yet.')
    template = yaml.safe_load((settings.BASE_DIR / 'deploy/customer-role.yaml').read_text())
    template['Parameters']['CollectorRoleArn']['Default'] = settings.COLLECTOR_ROLE_ARN
    template['Parameters']['ExternalId']['Default'] = str(customer.external_id)
    template['Parameters']['ExpectedAccountId']['Default'] = customer.account_id
    return yaml.safe_dump(template, sort_keys=False)


def quick_create_url(customer):
    if not settings.ARTIFACT_BUCKET or not settings.COLLECTOR_ROLE_ARN:
        return ''
    # New buckets may redirect the global S3 endpoint. A redirect changes the
    # signed host and invalidates the URL, so always sign the regional endpoint.
    s3 = boto3.client('s3', region_name=settings.AWS_REGION,
                      endpoint_url=f'https://s3.{settings.AWS_REGION}.amazonaws.com',
                      config=Config(signature_version='s3v4', s3={'addressing_style': 'virtual'}))
    # Generic template contains no credentials; presigned URL expires after one hour.
    template_url = s3.generate_presigned_url('get_object', Params={'Bucket': settings.ARTIFACT_BUCKET,
        'Key': 'templates/customer-role.yaml'}, ExpiresIn=3600)
    query = urlencode({'templateURL': template_url, 'stackName': 'CloudBillingReadOnly',
        'param_CollectorRoleArn': settings.COLLECTOR_ROLE_ARN, 'param_ExternalId': str(customer.external_id),
        'param_ExpectedAccountId': customer.account_id})
    return f'https://{settings.AWS_REGION}.console.aws.amazon.com/cloudformation/home?region={settings.AWS_REGION}#/stacks/create/review?{query}'
