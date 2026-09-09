from django.conf import settings
from django.core.management.base import BaseCommand,CommandError
from django.db import connection

class Command(BaseCommand):
    help='Read-only deployment gate for the actual runtime database identity and RLS.'
    def handle(self,*args,**options):
        expected={'web':'billing_web','collector':'billing_collector'}.get(settings.RUNTIME_ROLE)
        if not expected or connection.vendor!='postgresql' or not settings.DATABASE_RLS_ENABLED:
            raise CommandError('Use a configured PostgreSQL web/collector runtime with RLS enabled.')
        if not settings.ENFORCE_CUSTOMER_AUTHORIZATION or not settings.MFA_REQUIRED:
            raise CommandError('Authorization and MFA must be enabled.')
        with connection.cursor() as c:
            c.execute('SELECT current_user,rolsuper,rolbypassrls,rolcreatedb,rolcreaterole FROM pg_roles WHERE rolname=current_user')
            identity,*privileged=c.fetchone()
            c.execute("SELECT count(*) FROM pg_tables WHERE schemaname='public' AND tableowner=current_user")
            owns=c.fetchone()[0]
            c.execute("SELECT relrowsecurity FROM pg_class WHERE relname IN ('billing_customer','billing_cost','billing_explorerquery','billing_alliancerecord')")
            policies=c.fetchall()
            c.execute('SELECT count(*) FROM pg_auth_members WHERE member=(SELECT oid FROM pg_roles WHERE rolname=current_user)')
            memberships=c.fetchone()[0]
        if identity!=expected or any(privileged) or owns or memberships or len(policies)!=4 or not all(row[0] for row in policies):
            raise CommandError('Runtime role, ownership, membership or RLS configuration failed verification.')
        self.stdout.write('Actual runtime database identity and RLS configuration verified.')
