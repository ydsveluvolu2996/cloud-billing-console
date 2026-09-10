"""Individual authentication: local TOTP and pre-provisioned OIDC identities."""
import secrets
from datetime import timedelta
from django import forms
from django.conf import settings
from django.contrib.auth import logout, login as auth_login
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import User
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.crypto import constant_time_compare
from django.core.exceptions import PermissionDenied
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


PENDING_LOGIN_SECONDS = 300


def mfa_current(request):
    at = request.session.get('mfa_at', 0)
    return isinstance(at, (int, float)) and 0 <= timezone.now().timestamp() - at < settings.SESSION_COOKIE_AGE


def safe_next(request, value):
    return value if value and url_has_allowed_host_and_scheme(value, {request.get_host()}, require_https=request.is_secure()) else '/'


def begin_login(request, user, backend, destination='/'):
    # Only a short-lived challenge is saved. No authenticated user or password is
    # stored in the session until the second factor succeeds.
    profile, _ = UserSecurity.objects.get_or_create(user=user)
    if request.user.is_authenticated:
        logout(request)
    request.session.cycle_key()
    request.session['pending_login'] = {'user_id': user.pk, 'backend': backend,
        'version': profile.session_version, 'at': timezone.now().timestamp(),
        'auth_hash': user.get_session_auth_hash(),
        'next': safe_next(request, destination)}


def pending_identity(request):
    pending = request.session.get('pending_login', {})
    age = timezone.now().timestamp() - pending.get('at', 0)
    if not 0 <= age <= PENDING_LOGIN_SECONDS:
        request.session.pop('pending_login', None)
        return None
    user = User.objects.filter(pk=pending.get('user_id'), is_active=True).first()
    profile = UserSecurity.objects.filter(user=user).first() if user else None
    if (not profile or profile.session_version != pending.get('version')
            or not constant_time_compare(pending.get('auth_hash',''),user.get_session_auth_hash())
            or (profile.external and not settings.EXTERNAL_PORTAL_ENABLED)):
        request.session.pop('pending_login', None)
        return None
    return user


def verify_factor(request, user, token):
    """Row locks serialize TOTP replay checks and one-use recovery codes."""
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user.pk)
        profile = UserSecurity.objects.select_for_update().get(user=user)
        pending = request.session.get('pending_login',{})
        if (not user.is_active or profile.session_version != pending.get('version')
                or not constant_time_compare(pending.get('auth_hash',''),user.get_session_auth_hash())):
            request.session.pop('pending_login',None)
            raise PermissionDenied('This sign-in expired. Start again.')
        device = TOTPDevice.objects.select_for_update().filter(user=user, confirmed=True).first()
        enrolling = device is None
        if enrolling:
            device, _ = TOTPDevice.objects.get_or_create(user=user, name='primary', confirmed=False)
        allowed = not profile.recovery_locked_until or profile.recovery_locked_until <= timezone.now()
        verified = bool(token) and allowed and device.verify_token(token)
        recovery = False
        if not verified and allowed and not enrolling and len(token) >= 20:
            match = next((h for h in profile.recovery_hashes if check_password(token, h)), None)
            if match:
                profile.recovery_hashes.remove(match)
                recovery = verified = True
        codes = None
        if verified:
            profile.recovery_failed = 0
            profile.recovery_locked_until = None
            if enrolling:
                device.confirmed = True
                device.save(update_fields=['confirmed'])
                codes = [secrets.token_urlsafe(24) for _ in range(8)]
                profile.recovery_hashes = [make_password(code) for code in codes]
                security_event(user.username, 'MFA enrolled', target=str(user.pk))
            profile.save(update_fields=['recovery_hashes','recovery_failed','recovery_locked_until'])
            pending = request.session.pop('pending_login')
            auth_login(request, user, backend=pending['backend'])
            complete_mfa(request, profile, None if recovery else device)
            return True, codes, safe_next(request, pending.get('next'))
        if token:
            profile.recovery_failed += 1
            profile.recovery_locked_until = timezone.now() + timedelta(seconds=min(900, 2 ** min(profile.recovery_failed, 10)))
            profile.save(update_fields=['recovery_failed','recovery_locked_until'])
            security_event(user.username, 'MFA failed', outcome='denied', target=str(user.pk))
        return False, None, '/'


@never_cache
@sensitive_post_parameters()
def sign_in(request):
    if request.user.is_authenticated and (not settings.MFA_REQUIRED or mfa_current(request)):
        return redirect(safe_next(request, request.GET.get('next')))
    if request.method == 'POST' and request.POST.get('action') == 'restart':
        request.session.pop('pending_login', None)
        return redirect('login')
    user = pending_identity(request)
    error = ''
    form = AuthenticationForm(request, data=request.POST if request.method == 'POST' and not user else None)
    token = (request.POST.get('token') or '').strip()[:100]
    if request.method == 'POST' and not user:
        if form.is_valid():
            candidate = form.get_user()
            profile, _ = UserSecurity.objects.get_or_create(user=candidate)
            if profile.external and not settings.EXTERNAL_PORTAL_ENABLED:
                error = 'This account cannot sign in to this workspace.'
            elif not settings.MFA_REQUIRED:
                auth_login(request, candidate, backend=candidate.backend)
                return redirect(safe_next(request, request.POST.get('next')))
            else:
                begin_login(request, candidate, candidate.backend, request.POST.get('next', '/'))
                user = candidate
        else:
            error = 'Your username and password did not match, or sign-in is temporarily locked.'
    if user:
        if request.method == 'POST' and token:
            verified, codes, destination = verify_factor(request, user, token)
            if verified:
                if codes:
                    return render(request, 'registration/login.html', {'auth_screen': True,
                        'recovery_codes': codes, 'destination': destination})
                return redirect(destination)
            error = 'Code invalid, already used or temporarily rate-limited. Wait and try a fresh code.'
        with transaction.atomic():
            UserSecurity.objects.select_for_update().get(user=user)
            device = TOTPDevice.objects.filter(user=user, confirmed=True).first()
            enrolling = device is None
            if enrolling:
                device, _ = TOTPDevice.objects.get_or_create(user=user, name='primary', confirmed=False)
        return render(request, 'registration/login.html', {'auth_screen': True, 'challenge': True,
            'username': user.username, 'enrolling': enrolling, 'error': error,
            'enrollment_key': __import__('base64').b32encode(device.bin_key).decode() if enrolling else ''})
    return render(request, 'registration/login.html', {'auth_screen': True, 'form': form, 'error': error,
        'next': safe_next(request, request.GET.get('next', request.POST.get('next', '/')))})


@never_cache
def mfa(request):
    # Old bookmarks return to the sign-in flow; the dashboard has no MFA form.
    return redirect('login')


@require_POST
@login_required
def revoke_own_sessions(request):
    revoke_sessions(request.user, request.user.username)
    logout(request)
    return redirect('login')
