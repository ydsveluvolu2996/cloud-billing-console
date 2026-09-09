"""HTTP enforcement boundary; every request refreshes membership and session state."""
from django.conf import settings
from django.contrib.auth import logout
from django.db import connection, transaction
from django.http import HttpResponseForbidden
from django.shortcuts import redirect
from django.utils import timezone
from .access import context, for_user
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
            elif settings.MFA_REQUIRED and not public and request.path != '/mfa/':
                at = request.session.get('mfa_at', 0)
                if at < timezone.now().timestamp() - settings.SESSION_COOKIE_AGE:
                    return redirect('mfa')
        with transaction.atomic():
            if connection.vendor == 'postgresql' and settings.DATABASE_RLS_ENABLED:
                with connection.cursor() as cursor:
                    cursor.execute("SELECT set_config('billing.user_id', %s, true)", [str(request.user.pk) if request.user.is_authenticated else ''])
                    cursor.execute("SELECT set_config('billing.external_enabled', %s, true)", ['true' if settings.EXTERNAL_PORTAL_ENABLED else 'false'])
            access = for_user(request.user, write=request.method not in ('GET','HEAD','OPTIONS'))
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
