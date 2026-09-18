"""Dashboard intent and privileged, observable account connection verification.

The web only submits immutable requests through a guarded database function. The
administration service can invoke one constrained IAM broker; the web and cost
collector never receive IAM mutation permissions or administration credentials.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
from datetime import timedelta

import boto3
from botocore.config import Config
from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import DatabaseError, connection, transaction
from django.utils import timezone

from .access import context
from .authentication import security_event
from .iam import OPTIONAL, validate_role_arn
from .models import ActivationRequest, BillingSource, Customer, CustomerApproval, Job, RoleApproval, UserSecurity

ACTIVE_STATUSES = ('queued', 'processing', 'verifying', 'importing')
APPROVAL_FIELDS = ('contacts', 'authorized_users', 'expected_accounts', 'billing_fields', 'metadata',
                   'optional_capabilities', 'storage_region', 'retention_days', 'status', 'evidence', 'approved_by')
BILLING_FIELDS = ['day', 'account_id', 'service', 'unblended', 'amortized']


class ActivationInvalid(ValueError):
    """The frozen request is no longer authorized; a new request is necessary."""


def validate_automated_source(source):
    match = validate_role_arn(source.role_arn, source.account_id)
    if not match[3].startswith('BillingConsole/') or source.role_arn == settings.COLLECTOR_ROLE_ARN:
        raise ValidationError('Automatic connection requires a customer role under BillingConsole/.')
    if not source.enabled or not source.customer.active:
        raise ValidationError('Resume the connection and customer before connecting.')
    if source.shared:
        raise ValidationError('Shared payer connections require the reviewed administration workflow.')
    if source.kind not in (BillingSource.STANDALONE, BillingSource.MEMBER_BUDGETS):
        raise ValidationError('Automatic onboarding supports single accounts and member budget readers. Organization-wide payer connections require review.')
    if not isinstance(source.approved_capabilities, list) or any(c not in OPTIONAL for c in source.approved_capabilities):
        raise ValidationError('Choose only supported optional permissions.')
    if source.kind == BillingSource.STANDALONE and 'organizations' in source.approved_capabilities:
        raise ValidationError('Single-account connections must not request organization-wide inventory.')
    if source.kind == BillingSource.MEMBER_BUDGETS and source.approved_capabilities != ['budgets']:
        raise ValidationError('Member budget readers require only the Budgets optional permission.')
    if RoleApproval.objects.filter(source=source, role_arn=source.role_arn,
                                   connection_version=source.connection_version, status='revoked').exists():
        raise ValidationError('This role approval was revoked. An administrator must review reactivation.')


def normalized_consent(consent):
    if consent.get('confirmed') is not True:
        raise ValidationError('Confirm that you are authorized to connect this account and import its billing data.')
    values = {}
    for key, maximum in (('contact', 250), ('evidence', 500)):
        value = consent.get(key, '')
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
            raise ValidationError(f'Enter a valid {key.replace("_", " ")} (up to {maximum} characters).')
        values[key] = value.strip()
    days = consent.get('retention_days')
    if isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= 3650:
        raise ValidationError('Choose a billing retention period between 1 and 3650 days.')
    values.update(retention_days=days, confirmed=True)
    return values


def source_snapshot(source):
    return {'source_id': str(source.pk), 'customer_id': str(source.customer_id),
            'account_id': source.account_id, 'role_arn': source.role_arn,
            'external_id_sha256': hashlib.sha256(source.external_id.encode()).hexdigest(),
            'connection_version': source.connection_version, 'kind': source.kind,
            'shared': source.shared, 'approved_capabilities': sorted(set(source.approved_capabilities))}


def approval_snapshot(approval):
    return {key: getattr(approval, key) for key in APPROVAL_FIELDS} if approval else None


def role_approval_snapshot(source):
    return RoleApproval.objects.filter(source=source, role_arn=source.role_arn, connection_version=source.connection_version).values(
        'status', 'evidence', 'approved_by', 'requested_by').first()


def _administrator(user, session_version):
    from django.contrib.auth.models import User
    actor = User.objects.filter(pk=user.pk, is_active=True, is_superuser=True).first()
    profile = UserSecurity.objects.filter(user_id=user.pk, portfolio_access=True, external=False).first()
    if not actor or not profile or profile.session_version != session_version:
        raise PermissionDenied('A currently signed-in portfolio administrator must connect this account.')
    return actor, profile


def request_activation(source, user, consent, *, mfa_verified=False, session_version=None):
    """Called by the dashboard. PostgreSQL independently validates the request."""
    if not mfa_verified:
        raise PermissionDenied('Verify your authenticator before connecting this account.')
    actor, profile = _administrator(user, session_version)
    consent = normalized_consent(consent)
    validate_automated_source(source)
    if not getattr(settings, 'ONBOARDING_BROKER_FUNCTION', ''):
        raise ValidationError('Automatic onboarding is not configured on this dashboard yet.')
    with transaction.atomic():
        if connection.vendor == 'postgresql':
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute('SELECT billing_request_activation(%s, %s, %s::jsonb, %s, %s)',
                        [source.pk, session_version, json.dumps(consent), settings.AWS_REGION, settings.COLLECTOR_ROLE_ARN])
                    identifier = cursor.fetchone()[0]
            except DatabaseError as exc:
                code = getattr(exc.__cause__, 'sqlstate', None)
                if code == '42501':
                    raise PermissionDenied('A currently signed-in, MFA-verified administrator must connect this account.') from None
                if code == 'P0001':
                    raise ValidationError('The connection or customer approval changed. Review the existing consent, retention period and pending requests before trying again.') from None
                raise
            return ActivationRequest.objects.get(pk=identifier)
        if connection.vendor != 'sqlite':
            raise PermissionDenied('The activation request database boundary is unavailable.')
        # Equivalent development-only fallback. Production uses the guarded SQL
        # function; neither runtime role has INSERT/UPDATE on the request table.
        with context(None):
            source = BillingSource.objects.select_for_update().select_related('customer').get(pk=source.pk)
            validate_automated_source(source)
            approval = CustomerApproval.objects.filter(customer=source.customer).first()
            _check_existing_approval(approval, consent)
            existing = ActivationRequest.objects.filter(source=source, status__in=ACTIVE_STATUSES).first()
            if existing:
                if existing.requested_by_id == actor.pk and existing.session_version == session_version and existing.snapshot['source'] == source_snapshot(source) and existing.snapshot['consent'] == consent:
                    return existing
                raise ValidationError('A connection request is already running. Wait for its result before making another.')
            request = ActivationRequest.objects.create(source=source, requested_by=actor,
                session_version=profile.session_version, connection_version=source.connection_version,
                snapshot={'source': source_snapshot(source), 'approval': approval_snapshot(approval),
                          'role_approval': role_approval_snapshot(source), 'consent': consent, 'region': settings.AWS_REGION})
            security_event(actor.username, 'Account activation requested', customer=source.customer,
                source=source, target=str(request.pk), account_id=source.account_id, connection_version=source.connection_version)
            return request


def _check_existing_approval(approval, consent):
    if approval and approval.status == 'revoked':
        raise ValidationError('Customer approval was revoked. An administrator must review reactivation.')
    if approval and approval.status == 'approved':
        if approval.storage_region != settings.AWS_REGION or approval.retention_days != consent['retention_days']:
            raise ValidationError('Use the existing customer storage region and retention period when adding an account.')
        if not all([approval.contacts, approval.authorized_users, approval.billing_fields, approval.evidence,
                    approval.approved_by, approval.approved_at]):
            raise ValidationError('The existing customer approval needs administration review.')


def _revalidate(request, *, after_activation=False):
    source = BillingSource.objects.select_related('customer').get(pk=request.source_id)
    try:
        _administrator(request.requested_by, request.session_version)
        validate_automated_source(source)
        normalized_consent(request.snapshot['consent'])
    except (ValidationError, PermissionDenied) as exc:
        raise ActivationInvalid('Connection authorization changed. Review the account and submit a new request.') from exc
    if request.snapshot.get('source') != source_snapshot(source) or request.snapshot.get('region') != settings.AWS_REGION:
        raise ActivationInvalid('The connection changed. Review the account and submit a new request.')
    if not after_activation:
        if request.created_at < timezone.now() - timedelta(days=1):
            raise ActivationInvalid('This connection request expired. Submit it again from the dashboard.')
        approval = CustomerApproval.objects.filter(customer=source.customer).first()
        if approval_snapshot(approval) != request.snapshot.get('approval'):
            raise ActivationInvalid('Customer approval changed. Review it and submit a new request.')
        if role_approval_snapshot(source) != request.snapshot.get('role_approval'):
            raise ActivationInvalid('Role approval changed. Review it and submit a new request.')
        _check_existing_approval(approval, request.snapshot['consent'])
    else:
        from .iam import approval_ready
        if not approval_ready(source):
            raise ActivationInvalid('Connection approval was revoked or changed.')
    return source


def allow_role_via_broker(arn):
    """The AWS client can invoke only the fixed broker; it never calls IAM."""
    function = getattr(settings, 'ONBOARDING_BROKER_FUNCTION', '')
    if not function:
        raise ValueError('Automatic onboarding has not been configured by the dashboard administrator.')
    client = boto3.client('lambda', region_name=settings.AWS_REGION,
        config=Config(connect_timeout=5, read_timeout=40, retries={'max_attempts': 2, 'mode': 'standard'}))
    result = client.invoke(FunctionName=function, InvocationType='RequestResponse',
                           Payload=json.dumps({'role_arn': arn}).encode())
    payload = result.get('Payload')
    body = json.loads(payload.read(16385)) if payload else {}
    if result.get('FunctionError') or result.get('StatusCode') != 200 or body != {'role_arn': arn, 'status': 'allowed'}:
        raise ValueError('The onboarding authorizer could not allow this role. Retry from the dashboard.')


def merge_allowlist(arn):
    """Preserve root-owned metadata and replace the reviewed JSON atomically."""
    path = Path(settings.COLLECTOR_ALLOWLIST_FILE)
    if not settings.COLLECTOR_ALLOWLIST_FILE or path.is_symlink():
        raise ValueError('The collector allowlist path is not safe for automatic onboarding.')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise ValueError('The collector allowlist must be a protected regular file.')
    if os.geteuid() == 0 and info.st_uid != 0:
        raise ValueError('The collector allowlist must be owned by root.')
    entries = json.loads(path.read_text())
    if not isinstance(entries, list) or not all(isinstance(value, str) for value in entries):
        raise ValueError('The collector allowlist is invalid; existing entries were preserved.')
    for value in entries:
        validate_role_arn(value)
    if arn in entries:
        return
    descriptor, temporary = tempfile.mkstemp(prefix='.approved-roles-', dir=path.parent)
    try:
        os.fchmod(descriptor, stat.S_IMODE(info.st_mode))
        if os.geteuid() == 0:
            os.fchown(descriptor, info.st_uid, info.st_gid)
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(json.dumps(sorted(set(entries + [arn])), indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _approve(request, source):
    consent = request.snapshot['consent']
    actor = request.requested_by.username
    approval, _ = CustomerApproval.objects.get_or_create(customer=source.customer)
    if approval.status == 'approved':
        # Existing consent is never overwritten. The immutable request records
        # this additional account/capability approval and its supplied evidence.
        approval.expected_accounts = sorted(set(approval.expected_accounts + [source.account_id]))
        approval.optional_capabilities = sorted(set(approval.optional_capabilities + source.approved_capabilities))
        approval.save(update_fields=['expected_accounts', 'optional_capabilities', 'updated_at'])
    else:
        approval.contacts = [consent['contact']]
        approval.authorized_users = [actor]
        approval.expected_accounts = [source.account_id]
        approval.billing_fields = BILLING_FIELDS
        approval.metadata = []
        approval.optional_capabilities = list(source.approved_capabilities)
        approval.storage_region = request.snapshot['region']
        approval.retention_days = consent['retention_days']
        approval.status, approval.approved_by, approval.approved_at = 'approved', actor, timezone.now()
        approval.evidence = consent['evidence']
        approval.save()
    RoleApproval.objects.update_or_create(source=source, role_arn=source.role_arn,
        connection_version=source.connection_version, defaults={'requested_by': actor, 'status': 'approved',
            'evidence': consent['evidence'], 'approved_by': actor, 'approved_at': timezone.now()})


def _start(request):
    # The command takes a host-wide lock. Row locks serialize against edits,
    # offboarding and consent changes during the bounded broker invocation.
    from django.contrib.auth.models import User
    with transaction.atomic():
        request = ActivationRequest.objects.select_for_update().select_related('requested_by').get(pk=request.pk)
        User.objects.select_for_update().get(pk=request.requested_by_id)
        UserSecurity.objects.select_for_update().get(user_id=request.requested_by_id)
        BillingSource.objects.select_for_update().get(pk=request.source_id)
        source = _revalidate(request)
        Customer.objects.select_for_update().get(pk=source.customer_id)
        CustomerApproval.objects.select_for_update().filter(customer=source.customer).first()
        RoleApproval.objects.select_for_update().filter(source=source, role_arn=source.role_arn,
                                                       connection_version=source.connection_version).first()
        allow_role_via_broker(source.role_arn)
        source = _revalidate(request)
        merge_allowlist(source.role_arn)
        _approve(request, source)
        now = timezone.now()
        BillingSource.objects.filter(pk=source.pk).update(verified_at=None, discovered_at=None, trust_checks={}, last_error='')
        request.status, request.activated_at, request.last_error = 'verifying', now, ''
        request.save(update_fields=['status', 'activated_at', 'last_error', 'updated_at'])
        from .jobs import enqueue
        enqueue('verify', key=f'activation:{request.pk}:verify', source=source, priority=1, max_attempts=3,
            payload={'actor': request.requested_by.username, 'actor_id': request.requested_by_id,
                     'activation_id': str(request.pk)})
        security_event(request.requested_by.username, 'Account activation authorized', customer=source.customer,
            source=source, target=str(request.pk), account_id=source.account_id, connection_version=source.connection_version)


def _progress(request):
    with transaction.atomic():
        request = ActivationRequest.objects.select_for_update().select_related('requested_by').get(pk=request.pk)
        BillingSource.objects.select_for_update().get(pk=request.source_id)
        source = _revalidate(request, after_activation=True)
        if request.status == 'verifying':
            verify = Job.objects.filter(key=f'activation:{request.pk}:verify').order_by('-created_at').first()
            if verify is None:
                raise ActivationInvalid('Account verification was interrupted. Choose Connect account to retry.')
            if verify.status == Job.FAILED:
                raise ActivationInvalid(verify.last_error or 'AWS access verification failed. Check the customer role and try again.')
            if verify.status != Job.DONE:
                return
            checks = {'correct_external_id':'passed','missing_external_id':'denied','wrong_external_id':'denied',
                      'account_identity':'passed','exact_collector_principal':'passed','connection_version':source.connection_version}
            if not source.verified_at or source.verified_at < request.activated_at or any(source.trust_checks.get(k) != v for k, v in checks.items()):
                raise ActivationInvalid('The current customer role has not passed all trust checks.')
            if 'budgets' in source.approved_capabilities and source.capabilities.get('budgets') is not True:
                raise ActivationInvalid('AWS budget access is unavailable. Add the displayed budget permission and retry.')
            if source.collects_costs:
                if not source.discovered_at or source.discovered_at < request.activated_at:
                    # Verification queues discovery before it completes. A job
                    # coalesced with an earlier request may still be unstarted.
                    if Job.objects.filter(source=source, kind='discover', status__in=[Job.QUEUED, Job.LEASED]).exists():
                        return
                    discovery = Job.objects.filter(source=source, kind='discover', started_at__gte=request.activated_at).order_by('-created_at').first()
                    if discovery and discovery.status == Job.FAILED:
                        raise ActivationInvalid(discovery.last_error or 'Account discovery failed. Check customer permissions and try again.')
                    raise ActivationInvalid('Account discovery was interrupted. Choose Connect account to retry.')
            # Connecting proves access. The collector picks up this ready
            # connection on its next scheduler tick, including after a restart.
            # Data pulls use the same durable queue as recurring collection.
            request.status, request.finished_at = 'completed', timezone.now()
            security_event(request.requested_by.username, 'Account connection verified', customer=source.customer,
                source=source, target=str(request.pk), account_id=source.account_id)
        elif request.status == 'importing':
            # Finish requests already importing when this release was installed.
            required = (['initial'] if source.collects_costs else []) + (['budgets'] if 'budgets' in source.approved_capabilities else [])
            imports = [Job.objects.filter(source=source, key=f'activation:{request.pk}:{name}').order_by('-created_at').first() for name in required]
            failed = next((job for job in imports if job and job.status == Job.FAILED), None)
            if failed:
                raise ActivationInvalid(failed.last_error or 'Initial import failed. Check customer permissions and retry.')
            if any(job is None for job in imports):
                raise ActivationInvalid('The initial import was interrupted. Reconnect the account to resume automatic collection.')
            if not imports or any(job.status != Job.DONE for job in imports):
                return
            request.status, request.finished_at = 'completed', timezone.now()
            security_event(request.requested_by.username, 'Account activation completed', customer=source.customer,
                source=source, target=str(request.pk), account_id=source.account_id)
        request.save(update_fields=['status', 'finished_at', 'updated_at'])


def process_activation(request):
    if settings.RUNTIME_ROLE != 'admin':
        raise PermissionDenied('Use the separate activation administration runtime.')
    if request.status not in ACTIVE_STATUSES:
        return
    from django.db.models import F
    from .redaction import redact
    if request.status in ('queued', 'processing'):
        ActivationRequest.objects.filter(pk=request.pk).update(status='processing', attempts=F('attempts') + 1, updated_at=timezone.now())
    try:
        _start(request) if request.status in ('queued', 'processing') else _progress(request)
    except (ActivationInvalid, ValidationError, PermissionDenied) as exc:
        message = '; '.join(exc.messages) if isinstance(exc, ValidationError) else str(exc)
        ActivationRequest.objects.filter(pk=request.pk).update(status='failed', last_error=redact(message)[:500], finished_at=timezone.now(), updated_at=timezone.now())
        security_event('activation-service', 'Account activation rejected', source=request.source,
            customer=request.source.customer, target=str(request.pk), outcome='failed')
    except Exception:
        # Do not expose AWS payloads, local paths, credentials or role policy JSON.
        request.refresh_from_db()
        starting = request.status in ('queued', 'processing')
        attempts = request.attempts if starting else request.attempts + 1
        failed = attempts >= 5
        ActivationRequest.objects.filter(pk=request.pk).update(status='failed' if failed else 'queued' if starting else request.status, attempts=attempts,
            last_error='Connection authorization is temporarily unavailable. Retry from the dashboard.' if failed else 'Connection authorization will retry automatically.',
            finished_at=timezone.now() if failed else None, updated_at=timezone.now())
        security_event('activation-service', 'Account activation service retry', source=request.source,
            customer=request.source.customer, target=str(request.pk), outcome='failed')
