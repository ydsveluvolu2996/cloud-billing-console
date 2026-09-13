"""OIDC login for administrator-provisioned (issuer, subject) identities only.

Local MFA is still required after SSO. No email-based linking or JIT provisioning.
"""
from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from mozilla_django_oidc.auth import OIDCAuthenticationBackend


class BillingOIDCBackend(OIDCAuthenticationBackend):
    def authenticate(self, request, **kwargs):
        if not settings.OIDC_ENABLED:
            return None
        for endpoint in (settings.OIDC_ISSUER, settings.OIDC_OP_AUTHORIZATION_ENDPOINT,
                         settings.OIDC_OP_TOKEN_ENDPOINT, settings.OIDC_OP_JWKS_ENDPOINT):
            if not endpoint.startswith('https://'):
                raise SuspiciousOperation('OIDC requires configured HTTPS endpoints.')
        return super().authenticate(request, **kwargs)

    def verify_token(self, token, **kwargs):
        payload = super().verify_token(token, **kwargs)
        aud = payload.get('aud', [])
        aud = [aud] if isinstance(aud, str) else aud
        if (payload.get('iss') != settings.OIDC_ISSUER or settings.OIDC_RP_CLIENT_ID not in aud
                or not payload.get('sub') or not payload.get('exp') or not payload.get('iat')
                or (len(aud) > 1 and payload.get('azp') != settings.OIDC_RP_CLIENT_ID)):
            raise SuspiciousOperation('OIDC issuer, audience or required claims mismatch.')
        return payload

    def get_or_create_user(self, access_token, id_token, payload):
        return self.UserModel.objects.filter(is_active=True, security__oidc_subject=payload['sub'],
                                             security__oidc_issuer=payload['iss']).first()
