"""Manual role policies and fail-closed collector allowlisting.

The provider policy deliberately has no ExternalId condition: negative trust probes
must test the customer's trust, not a provider-side denial masking a bad trust.
"""
import json
import re
import secrets
from pathlib import Path
from urllib.parse import unquote
from django.conf import settings
from django.core.exceptions import ValidationError
from django.utils import timezone
from botocore.exceptions import ClientError

ROLE_RE = re.compile(r'^arn:(aws|aws-us-gov|aws-cn):iam::([0-9]{12}):role/([\x21-\x7e]+)$')
OPTIONAL = {
    'organizations': ['organizations:DescribeOrganization', 'organizations:ListAccounts'],
    'tags': ['ce:GetTags', 'ce:ListCostAllocationTags'],
    'cost_categories': ['ce:GetCostCategories'],
    'forecasts': ['ce:GetCostForecast'],
    'resources': ['ce:GetCostAndUsageWithResources'],
    'budgets': ['budgets:ViewBudget'],
}


def validate_role_arn(arn, account_id=None):
    match = ROLE_RE.fullmatch(arn or '')
    if not match or any(c in arn for c in '*?') or not re.fullmatch(r'[\w+=,.@-]{1,64}', match[3].split('/')[-1], re.ASCII):
        raise ValidationError({'role_arn': 'Enter an exact IAM role ARN with a valid role name and path; wildcards are not allowed.'})
    if account_id and match[2] != account_id:
        raise ValidationError({'role_arn': 'The role ARN must match the registered 12-digit AWS account.'})
    if match[1] not in settings.SUPPORTED_AWS_PARTITIONS:
        raise ValidationError({'role_arn': 'This AWS partition is not supported by the configured collector.'})
    collector = ROLE_RE.fullmatch(settings.COLLECTOR_ROLE_ARN or '')
    if collector and collector[1] != match[1]:
        raise ValidationError({'role_arn': 'Customer and collector must use the same AWS partition.'})
    return match


def policy_bundle(source):
    if not settings.COLLECTOR_ROLE_ARN:
        raise ValueError('An administrator must configure the exact collector IAM role before generating trust JSON.')
    validate_role_arn(settings.COLLECTOR_ROLE_ARN)
    arn = source.role_arn or source.expected_role_arn
    parsed = validate_role_arn(arn, source.account_id)
    trust = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Principal': {'AWS': settings.COLLECTOR_ROLE_ARN},
             'Action': 'sts:AssumeRole', 'Condition': {'StringEquals': {'sts:ExternalId': str(source.external_id)}}}]}
    required = {'Version': '2012-10-17', 'Statement': [
        {'Sid': 'CoreBilling', 'Effect': 'Allow', 'Action': ['ce:GetCostAndUsage', 'ce:GetDimensionValues'], 'Resource': '*'},
        {'Sid': 'VerifyOwnTrust', 'Effect': 'Allow', 'Action': 'iam:GetRole', 'Resource': arn}]}
    optional = {}
    for capability, actions in OPTIONAL.items():
        resource = f'arn:{parsed[1]}:budgets::{source.account_id}:budget/*' if capability == 'budgets' else '*'
        optional[capability] = {'Version': '2012-10-17', 'Statement': [{'Effect': 'Allow', 'Action': actions, 'Resource': resource}]}
    return {'role_arn': arn, 'trust_policy': trust, 'minimum_permission_policy': required,
            'optional_permission_policies': optional, 'external_id': str(source.external_id)}


def request_allowlist(source, actor):
    from .models import RoleApproval
    validate_role_arn(source.role_arn, source.account_id)
    approval, _ = RoleApproval.objects.get_or_create(source=source, role_arn=source.role_arn, connection_version=source.connection_version,
                                                    defaults={'requested_by': actor})
    return approval


def approval_ready(source):
    from .models import CustomerApproval, RoleApproval
    approval = CustomerApproval.objects.filter(customer=source.customer, status='approved').first()
    if not approval or not all([approval.evidence, approval.contacts, approval.billing_fields, approval.storage_region,
                                approval.retention_days, approval.approved_at, approval.approved_by]):
        return False
    if (approval.storage_region != settings.AWS_REGION or source.account_id not in approval.expected_accounts
            or not set(source.approved_capabilities).issubset(set(approval.optional_capabilities))):
        return False
    return RoleApproval.objects.filter(source=source, role_arn=source.role_arn, connection_version=source.connection_version,
                                       status='approved', approved_at__isnull=False).exclude(evidence='').exists()


def assert_role_allowed(source):
    if settings.RUNTIME_ROLE != 'collector':
        raise ValueError('Customer AWS access is restricted to the separate collector runtime.')
    validate_role_arn(source.role_arn, source.account_id)
    if not source.customer.active or not source.enabled or not approval_ready(source):
        raise ValueError('Customer approval and the exact role allowlist approval are required before AWS access.')
    path = settings.COLLECTOR_ALLOWLIST_FILE
    if not path:
        raise ValueError('The collector deployment allowlist is not configured.')
    try:
        approved = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise ValueError('The collector deployment allowlist cannot be read.') from None
    if not isinstance(approved, list) or source.role_arn not in approved:
        raise ValueError('This exact customer role ARN is not in the deployed collector allowlist.')


def verify_trust(source, session, sts, meter):
    """Strict supported trust form, plus successful and explicit-negative STS probes."""
    validate_role_arn(source.role_arn, source.account_id)
    identity = meter.call(session.client('sts'), 'get_caller_identity')
    expected_prefix = f'arn:{source.role_arn.split(":")[1]}:sts::{source.account_id}:assumed-role/'
    if identity.get('Account') != source.account_id or not identity.get('Arn', '').startswith(expected_prefix):
        raise ValueError('Assumed caller identity does not match the registered customer account.')
    result = meter.call(session.client('iam'), 'get_role', RoleName=source.role_arn.rsplit('/', 1)[-1])['Role']
    if result.get('Arn') != source.role_arn:
        raise ValueError('Returned IAM role does not match the approved ARN.')
    policy = result.get('AssumeRolePolicyDocument', {})
    if isinstance(policy, str):
        policy = json.loads(unquote(policy))
    expected = policy_bundle(source)['trust_policy']['Statement'][0]
    statements = policy.get('Statement', [])
    if isinstance(statements, dict):
        statements = [statements]
    # Reject broad principals, alternative allow statements, NotPrincipal/NotAction,
    # StringLike and conditional forms that could permit an ExternalId bypass.
    allows = [s for s in statements if s.get('Effect') == 'Allow']
    normalized = [{k: v for k, v in s.items() if k != 'Sid'} for s in allows]
    if normalized != [expected]:
        raise ValueError('Trust must contain exactly the supplied allow statement for our collector and unique External ID. Review additional or broad trust statements.')
    for label, external_id in [('missing_external_id', None), ('wrong_external_id', secrets.token_hex(24))]:
        args = {'RoleArn': source.role_arn, 'RoleSessionName': 'billing-trust-validation', 'DurationSeconds': 900}
        if external_id is not None:
            args['ExternalId'] = external_id
        try:
            meter.call(sts, 'assume_role', **args)
        except ClientError as exc:
            if exc.response.get('Error', {}).get('Code') not in ('AccessDenied', 'AccessDeniedException'):
                raise ValueError(f'{label}: AWS returned an inconclusive error; explicit AccessDenied is required.') from None
        else:
            raise ValueError(f'{label}: role assumption succeeded. Correct the customer trust before activation.')
    return {'correct_external_id': 'passed', 'missing_external_id': 'denied', 'wrong_external_id': 'denied',
            'account_identity': 'passed', 'exact_collector_principal': 'passed',
            'connection_version': source.connection_version, 'checked_at': timezone.now().isoformat()}
