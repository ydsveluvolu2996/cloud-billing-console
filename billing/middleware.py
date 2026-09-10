"""HTTP enforcement boundary; every request refreshes membership and session state."""
from django.conf import settings
from django.contrib.auth import logout
from django.db import connection, transaction
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.utils import timezone
from .access import context, for_user, scope_fingerprint
from .models import UserSecurity


class SecurityMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        public = request.path in ('/login/','/logout/','/health/') or request.path.startswith('/oidc/')
        if request.user.is_authenticated:
            profile, _ = UserSecurity.objects.get_or_create(user=request.user)
            if request.session.get('security_version') != profile.session_version:
                logout(request)
                if not public:
                    return redirect('login')
            elif profile.external and not settings.EXTERNAL_PORTAL_ENABLED and not public:
                return HttpResponseForbidden('External customer access is disabled.')
            elif settings.MFA_REQUIRED and request.path != '/logout/' and not request.path.startswith('/oidc/'):
                from .authentication import mfa_current, begin_login
                if not mfa_current(request):
                    user = request.user
                    backend = request.session.get('_auth_user_backend', 'django.contrib.auth.backends.ModelBackend')
                    begin_login(request, user, backend, request.get_full_path() if request.path not in ('/login/','/mfa/') else '/')
                    return redirect('login')
        with transaction.atomic():
            if connection.vendor == 'postgresql' and settings.DATABASE_RLS_ENABLED:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('billing.user_id', %s, true)", [str(request.user.pk) if request.user.is_authenticated else ''])
                    cursor.execute("SELECT set_config('billing.external_enabled', %s, true)", ['true' if settings.EXTERNAL_PORTAL_ENABLED else 'false'])
                    from .authentication import mfa_current
                    cursor.execute("SELECT set_config('billing.mfa_verified', %s, true)", ['true' if request.user.is_authenticated and mfa_current(request) else 'false'])
            access = for_user(request.user, write=request.method not in ('GET','HEAD','OPTIONS'))
            if connection.vendor == 'postgresql' and settings.DATABASE_RLS_ENABLED:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('billing.scope_fingerprint', %s, true)", [scope_fingerprint(access)])
            with context(access):
                if settings.ENFORCE_CUSTOMER_AUTHORIZATION and request.path.startswith('/admin/') and not access.portfolio:
                    return HttpResponseForbidden('Explicit portfolio administrator authorization is required.')
                response = self.get_response(request)
                # Materialize templates/streams while authorization context still exists.
                if hasattr(response, 'render') and not response.is_rendered:
                    response.render()
                if getattr(response, 'streaming', False):
                    from django.http import HttpResponse
                    response = HttpResponse(b''.join(response.streaming_content),status=response.status_code,headers=dict(response.headers))
                if request.user.is_authenticated and response.status_code < 400 and ('attachment' in response.get('Content-Disposition', '')):
                    from .authentication import security_event
                    security_event(request.user.username, 'Export downloaded', target=request.resolver_match.url_name if request.resolver_match else 'download',
                                   bytes=len(response.content), scope=[str(x) for x in access.customers])
                response['Referrer-Policy'] = 'same-origin'
                response['Permissions-Policy'] = 'camera=(), microphone=(), geolocation=()'
                if request.path != '/health/':
                    response['Cache-Control'] = 'no-store, private'
                return response
