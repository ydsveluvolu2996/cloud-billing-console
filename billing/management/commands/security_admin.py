"""Evidence-backed administrative changes. Never modifies AWS IAM."""
import json
from pathlib import Path
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from billing.models import Customer, CustomerApproval, BillingSource, RoleApproval, UserSecurity, CustomerMembership
from billing.authentication import security_event, revoke_sessions
from billing.iam import validate_role_arn


class Command(BaseCommand):
    help = 'Admin-only grant/revoke, approve/revoke customer or role, allowlist export, and audited MFA recovery.'
    def add_arguments(self, parser):
        parser.add_argument('action', choices=['grant','revoke','portfolio','approve-customer','revoke-customer','approve-role','revoke-role','export-allowlist','recover-mfa','support-access'])
        parser.add_argument('--username')
        parser.add_argument('--customer')
        parser.add_argument('--source')
        parser.add_argument('--role', choices=['operator','viewer','customer'], default='viewer')
        parser.add_argument('--accounts', default='')
        parser.add_argument('--evidence', required=True)
        parser.add_argument('--actor', required=True)
        parser.add_argument('--output')
        parser.add_argument('--expires-hours',type=int,default=1)
    @transaction.atomic
    def handle(self, *args, **opts):
        if settings.RUNTIME_ROLE != 'admin':
            raise CommandError('Use the separate administration runtime and migration/administration database identity.')
        if not opts['evidence'].strip():
            raise CommandError('An evidence reference is required.')
        action, actor = opts['action'], opts['actor']
        customer = Customer.objects.get(pk=opts['customer']) if opts['customer'] else None
        user = User.objects.get(username=opts['username']) if opts['username'] else None
        source = BillingSource.objects.get(pk=opts['source']) if opts['source'] else None
        if action == 'support-access':
            if not customer or not user or not 1<=opts['expires_hours']<=8:
                raise CommandError('Customer, individual user and a 1–8 hour support window are required.')
            from datetime import timedelta
            CustomerMembership.objects.update_or_create(user=user,customer=customer,defaults={'role':'viewer','active':True,'expires_at':timezone.now()+timedelta(hours=opts['expires_hours']),'support_reason':opts['evidence']})
        elif action in ('grant','revoke'):
            if not customer or not user:
                raise CommandError('--customer and --username are required.')
            accounts = sorted(set(x.strip() for x in opts['accounts'].split(',') if x.strip()))
            owned = set(customer.assignments.values_list('account__account_id',flat=True))
            if not set(accounts).issubset(owned):
                raise CommandError('Every restricted account must be assigned to this customer.')
            CustomerMembership.objects.update_or_create(user=user,customer=customer,defaults={'role':opts['role'],'active':action=='grant','account_ids':accounts})
        elif action == 'portfolio':
            if not user or not user.is_superuser:
                raise CommandError('Explicit portfolio access requires an individual superuser identity.')
            UserSecurity.objects.update_or_create(user=user,defaults={'portfolio_access':True})
            revoke_sessions(user,actor,opts['evidence'])
        elif action in ('approve-customer','revoke-customer'):
            if not customer:
                raise CommandError('--customer is required.')
            approval, _ = CustomerApproval.objects.get_or_create(customer=customer)
            if action == 'approve-customer':
                if not all([approval.contacts,approval.expected_accounts,approval.billing_fields,approval.storage_region,approval.retention_days,approval.authorized_users]):
                    raise CommandError('Contacts, authorized users, inventory, billing fields, region and retention must be supplied.')
                if approval.storage_region != settings.AWS_REGION:
                    raise CommandError('Approval storage region must match this deployment.')
                approval.status, approval.approved_by, approval.approved_at = 'approved',actor,timezone.now()
            else:
                approval.status = 'revoked'
                BillingSource.objects.filter(customer=customer).update(enabled=False, verified_at=None)
            approval.evidence=opts['evidence']; approval.save()
        elif action in ('approve-role','revoke-role'):
            if not source:
                raise CommandError('--source is required.')
            validate_role_arn(source.role_arn,source.account_id)
            role, _ = RoleApproval.objects.get_or_create(source=source,role_arn=source.role_arn,connection_version=source.connection_version,defaults={'requested_by':actor})
            role.status = 'approved' if action=='approve-role' else 'revoked'
            role.approved_by, role.approved_at, role.evidence = actor,timezone.now(),opts['evidence']; role.save()
            if action == 'revoke-role':
                BillingSource.objects.filter(pk=source.pk).update(enabled=False,verified_at=None)
        elif action == 'recover-mfa':
            if not user:
                raise CommandError('--username is required after out-of-band identity verification.')
            from django_otp.plugins.otp_totp.models import TOTPDevice
            TOTPDevice.objects.filter(user=user).delete()
            profile,_ = UserSecurity.objects.get_or_create(user=user)
            profile.recovery_hashes=[]; profile.save(update_fields=['recovery_hashes'])
            revoke_sessions(user,actor,opts['evidence'])
        elif action == 'export-allowlist':
            if not opts['output']:
                raise CommandError('--output is required. The command prepares files; AWS changes are separate.')
            roles=sorted(set(RoleApproval.objects.filter(status='approved',role_arn=F('source__role_arn'),connection_version=F('source__connection_version'),source__enabled=True,source__customer__active=True,source__customer__approval__status='approved').values_list('role_arn',flat=True)))
            for arn in roles:
                validate_role_arn(arn)
            path=Path(opts['output']); path.write_text(json.dumps(roles,indent=2)+'\n');path.chmod(0o600)
            statements=[{'Effect':'Allow','Action':'sts:AssumeRole','Resource':roles}] if roles else [{'Effect':'Deny','Action':'sts:AssumeRole','Resource':'*'}]
            policy=path.with_suffix('.policy.json');policy.write_text(json.dumps({'Version':'2012-10-17','Statement':statements},indent=2)+'\n');policy.chmod(0o600)
        security_event(actor,'Administrative '+action,customer=customer,target=str(source.pk if source else user.pk if user else ''),evidence=opts['evidence'])
        self.stdout.write('Administrative record saved. No AWS permissions or live resources were changed.')
