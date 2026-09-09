"""Individual authentication: local TOTP and pre-provisioned OIDC identities."""
import secrets
from datetime import timedelta
from django import forms
from django.conf import settings
from django.contrib.auth import logout
from django.contrib.auth.decorators import login_required
from django.contrib.auth.hashers import make_password, check_password
from django.db import transaction
from django.db.models import F
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache
from django.views.decorators.debug import sensitive_post_parameters
from django.views.decorators.http import require_POST
from django_otp import login as otp_login
from django_otp.plugins.otp_totp.models import TOTPDevice
from .models import UserSecurity


class TokenForm(forms.Form):
    token = forms.CharField(label='Authenticator or recovery code', max_length=100,
                           widget=forms.TextInput(attrs={'autocomplete':'one-time-code', 'autofocus': True}))


def security_event(actor, action, customer=None, outcome='success', target='', source=None, **details):
    from .models import AuditEvent
    from .access import context
    from django.db import connection
    from .redaction import audit_details
    payload=audit_details({'outcome':outcome,'target':target,**details})
    with context(None):
        if connection.vendor=='postgresql' and settings.DATABASE_RLS_ENABLED:
            # INSERT without RETURNING does not need SELECT visibility for global
            # authentication events. Runtime audit privileges remain append-only.
            import json,logging
            now=timezone.now()
            with connection.cursor() as cursor:
                cursor.execute("SELECT nextval(pg_get_serial_sequence('billing_auditevent','id'))")
                identifier=cursor.fetchone()[0]
                cursor.execute('INSERT INTO billing_auditevent(id,at,actor,action,customer_id,source_id,details) VALUES (%s,%s,%s,%s,%s,%s,%s)',
                    [identifier,now,actor,action,customer.pk if customer else None,source.pk if source else None,json.dumps(payload)])
            logging.getLogger('security.audit').info(json.dumps({'id':identifier,'at':now.isoformat(),'actor':actor,'customer':str(customer.pk) if customer else '', 'action':action,'details':payload}))
            return None
        return AuditEvent.objects.create(actor=actor,action=action,customer=customer,source=source,details=payload)


def revoke_sessions(user, actor, evidence=''):
    profile, _ = UserSecurity.objects.get_or_create(user=user)
    UserSecurity.objects.filter(pk=profile.pk).update(session_version=F('session_version') + 1)
    security_event(actor, 'Sessions revoked', target=str(user.pk), evidence=evidence)


def complete_mfa(request, profile, device=None):
    request.session.cycle_key()
    request.session['security_version'] = profile.session_version
    request.session['mfa_at'] = timezone.now().timestamp()
    if device:
        otp_login(request, device)
    security_event(request.user.username, 'MFA verified', target=str(request.user.pk))


@never_cache
@sensitive_post_parameters()
@login_required
def mfa(request):
    form = TokenForm(request.POST or None)
    codes = None
    with transaction.atomic():
        profile, _ = UserSecurity.objects.select_for_update().get_or_create(user=request.user)
        device = TOTPDevice.objects.select_for_update().filter(user=request.user, confirmed=True).first()
        enrolling = device is None
        if enrolling:
            device, _ = TOTPDevice.objects.get_or_create(user=request.user, name='primary', confirmed=False)
        if request.method == 'POST' and form.is_valid():
            token = form.cleaned_data['token'].strip()
            allowed = not profile.recovery_locked_until or profile.recovery_locked_until <= timezone.now()
            verified = allowed and device.verify_token(token)
            recovery = False
            if not verified and allowed and not enrolling and len(token) >= 20:
                match = next((h for h in profile.recovery_hashes if check_password(token, h)), None)
                if match:
                    profile.recovery_hashes.remove(match)
                    recovery = verified = True
            if verified:
                profile.recovery_failed = 0
                profile.recovery_locked_until = None
                if enrolling:
                    device.confirmed = True
                    device.save(update_fields=['confirmed'])
                    codes = [secrets.token_urlsafe(24) for _ in range(8)]
                    profile.recovery_hashes = [make_password(code) for code in codes]
                    security_event(request.user.username, 'MFA enrolled', target=str(request.user.pk))
                profile.save(update_fields=['recovery_hashes','recovery_failed','recovery_locked_until'])
                complete_mfa(request, profile, None if recovery else device)
                if not codes:
                    return redirect('dashboard')
            else:
                profile.recovery_failed += 1
                profile.recovery_locked_until = timezone.now() + timedelta(seconds=min(900, 2 ** min(profile.recovery_failed, 10)))
                profile.save(update_fields=['recovery_failed','recovery_locked_until'])
                security_event(request.user.username, 'MFA failed', outcome='denied', target=str(request.user.pk))
                form.add_error('token', 'Code invalid, already used or temporarily rate-limited. Wait and try a fresh code.')
    return render(request, 'registration/mfa.html', {'form': form, 'enrolling': enrolling, 'device': device if enrolling else None, 'enrollment_key': __import__('base64').b32encode(device.bin_key).decode() if enrolling else '', 'recovery_codes': codes})


@require_POST
@login_required
def revoke_own_sessions(request):
    revoke_sessions(request.user, request.user.username)
    logout(request)
    return redirect('login')
