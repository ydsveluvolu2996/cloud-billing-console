"""Execute the installed migration SQL unchanged against disposable TLS PostgreSQL.

Opt in with RUN_RELEASE_SQL_TESTS=1. This fixture never reads DATABASE_URL,
application credentials, production certificates, or an existing database.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DIRECTORY))
import additive_migrations as additive

NAME = '0022_savedreport_archived_at'
PLAN = {'version': 1, 'name': NAME, 'dependency': '0021_activationrequest',
        'prior': ['0021_activationrequest'], 'table': 'billing_savedreport', 'column': 'archived_at'}
ADMIN_PASSWORD = 'synthetic-billing-sql-admin-only'
BOOTSTRAP_PASSWORD = 'synthetic-billing-sql-bootstrap-only'
DATABASE = 'billing_sql_integration'

OPENSSL_CONFIG = """[req]
distinguished_name = name
x509_extensions = server
prompt = no
[name]
CN = localhost
[server]
subjectAltName = DNS:localhost,IP:127.0.0.1
basicConstraints = critical,CA:TRUE
keyUsage = critical,digitalSignature,keyEncipherment,keyCertSign
extendedKeyUsage = serverAuth
"""

STARTUP = """mkdir -p /tmp/release-test-tls
cp /fixtures/server.crt /fixtures/server.key /tmp/release-test-tls/
chown -R postgres:postgres /tmp/release-test-tls
chmod 700 /tmp/release-test-tls
chmod 600 /tmp/release-test-tls/server.key
exec docker-entrypoint.sh postgres -c ssl=on \
  -c ssl_cert_file=/tmp/release-test-tls/server.crt \
  -c ssl_key_file=/tmp/release-test-tls/server.key \
  -c listen_addresses=localhost
"""


class SQLIntegrationGate(unittest.TestCase):
    def test_ci_matrix_requires_real_sql_execution(self):
        import yaml
        workflow = yaml.safe_load((DIRECTORY.parents[1] / '.github/workflows/checks.yml').read_text())
        job = workflow['jobs']['test']
        self.assertEqual(job['env']['RUN_RELEASE_SQL_TESTS'], '1')
        self.assertEqual(job['env']['RELEASE_SQL_POSTGRES_IMAGE'], 'postgres:${{ matrix.postgres }}')
        self.assertEqual(set(job['strategy']['matrix']['postgres']), {'17-alpine', '18-alpine'})
        step = next(item for item in job['steps']
                    if 'unittest discover -s deploy/github-release/tests' in item.get('run', ''))
        self.assertNotIn('continue-on-error', step)
        self.assertNotIn('continue-on-error', job)


@unittest.skipUnless(os.environ.get('RUN_RELEASE_SQL_TESTS') == '1',
                     'Set RUN_RELEASE_SQL_TESTS=1 for the disposable Docker/TLS SQL fixture')
class AdditiveSQLPostgres(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('docker') or not shutil.which('openssl'):
            raise RuntimeError('Opted-in release SQL tests require Docker and openssl')
        image = os.environ.get('RELEASE_SQL_POSTGRES_IMAGE', 'postgres:17-alpine')
        if image not in ('postgres:17-alpine', 'postgres:18-alpine'):
            raise ValueError('Release SQL fixture requires an official supported PostgreSQL image')
        cls.container = 'billing-release-sql-' + uuid.uuid4().hex[:16]
        temporary = tempfile.TemporaryDirectory(prefix='billing-release-sql-')
        cls.addClassCleanup(temporary.cleanup)
        fixture = Path(temporary.name)
        config = fixture / 'openssl.cnf'
        config.write_text(OPENSSL_CONFIG)
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '2',
                        '-config', str(config), '-keyout', str(fixture / 'server.key'),
                        '-out', str(fixture / 'server.crt')], check=True, capture_output=True, text=True, timeout=30)
        cls.addClassCleanup(cls.remove_container)
        subprocess.run(['docker', 'run', '--detach', '--rm', '--name', cls.container, '--network', 'none',
                        '--memory', '512m', '--pids-limit', '128',
                        '--tmpfs', '/var/lib/postgresql:rw,nosuid,nodev,size=256m',
                        '--mount', 'type=bind,src=' + str(fixture) + ',dst=/fixtures,readonly',
                        '-e', 'POSTGRES_PASSWORD=' + BOOTSTRAP_PASSWORD,
                        '-e', 'POSTGRES_DB=' + DATABASE, '-e', 'PGDATA=/var/lib/postgresql/test-data',
                        '-e', 'POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256 --auth-local=trust',
                        '--entrypoint', 'sh', image, '-ec', STARTUP],
                       check=True, capture_output=True, text=True, timeout=180)
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            ready = subprocess.run(['docker', 'exec', cls.container, 'pg_isready', '-h', 'localhost',
                                    '-U', 'postgres', '-d', DATABASE],
                                   capture_output=True, text=True, timeout=10)
            if ready.returncode == 0:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError('Disposable TLS PostgreSQL did not become ready')
        cls.psql("""CREATE ROLE billing_admin LOGIN PASSWORD '""" + ADMIN_PASSWORD + """'
                    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
          CREATE ROLE billing_web NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
          CREATE ROLE billing_collector NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS;
          ALTER DATABASE """ + DATABASE + """ OWNER TO billing_admin;
          GRANT USAGE,CREATE ON SCHEMA public TO billing_admin;
          """, user='postgres')

    @classmethod
    def remove_container(cls):
        subprocess.run(['docker', 'rm', '--force', cls.container],
                       capture_output=True, text=True, timeout=30, check=False)

    @classmethod
    def psql(cls, sql, *, user='billing_admin', sslmode='verify-full', check=True):
        password = ADMIN_PASSWORD if user == 'billing_admin' else BOOTSTRAP_PASSWORD
        result = subprocess.run(['docker', 'exec', '-i', '-e', 'PGPASSWORD=' + password,
                                 '-e', 'PGSSLMODE=' + sslmode,
                                 '-e', 'PGSSLROOTCERT=/tmp/release-test-tls/server.crt',
                                 '-e', 'PGCONNECT_TIMEOUT=5', cls.container,
                                 'psql', '-X', '-qAt', '-v', 'ON_ERROR_STOP=1',
                                 '-h', 'localhost', '-U', user, '-d', DATABASE],
                                input=sql, capture_output=True, text=True, timeout=90)
        if check and result.returncode:
            raise AssertionError('Synthetic PostgreSQL fixture command failed:\n' + result.stderr)
        return result

    def setUp(self):
        self.psql("""DROP TABLE IF EXISTS public.billing_savedreport, public.django_migrations CASCADE;
          DROP FUNCTION IF EXISTS public.fixture_mutate_after_history();
          CREATE TABLE public.billing_savedreport(id bigint PRIMARY KEY, name text, params jsonb);
          INSERT INTO public.billing_savedreport VALUES
            (1,'Synthetic report','{"metric":"UnblendedCost"}'),(2,'Second report','{"nested":{"value":7}}');
          CREATE TABLE public.django_migrations(id bigserial PRIMARY KEY, app text, name text, applied timestamptz);
          INSERT INTO public.django_migrations(app,name,applied)
            VALUES('billing','0021_activationrequest','2026-01-01T00:00:00Z'),
                  ('auth','0012_alter_user_first_name_max_length','2026-01-01T00:00:00Z');
          ALTER TABLE public.billing_savedreport ENABLE ROW LEVEL SECURITY;
          CREATE POLICY web_scope ON public.billing_savedreport TO billing_web USING(id>0) WITH CHECK(id>0);
          CREATE POLICY collector_scope ON public.billing_savedreport FOR SELECT TO billing_collector USING(id>0);
          GRANT SELECT,INSERT,UPDATE,DELETE ON public.billing_savedreport TO billing_web;
          GRANT UPDATE(name) ON public.billing_savedreport TO billing_web;
          GRANT SELECT ON public.billing_savedreport TO billing_collector;
          """)

    def snapshot(self):
        result = self.psql("""SELECT jsonb_build_object(
          'rows',(SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM public.billing_savedreport t),
          'history',(SELECT jsonb_agg(to_jsonb(t) ORDER BY id) FROM public.django_migrations t),
          'columns',(SELECT jsonb_agg(jsonb_build_object('name',attname,'type',format_type(atttypid,atttypmod),
             'not_null',attnotnull,'default',atthasdef,'identity',attidentity,'generated',attgenerated,
             'acl',attacl::text) ORDER BY attnum) FROM pg_attribute
             WHERE attrelid='public.billing_savedreport'::regclass AND attnum>0 AND NOT attisdropped),
          'access',jsonb_build_object(
            'table',(SELECT jsonb_build_object('owner',pg_get_userbyid(relowner),'rls',relrowsecurity,
              'force',relforcerowsecurity,'acl',relacl::text) FROM pg_class WHERE oid='public.billing_savedreport'::regclass),
            'policies',(SELECT jsonb_agg(to_jsonb(p) ORDER BY policyname) FROM pg_policies p
              WHERE schemaname='public' AND tablename='billing_savedreport'),
            'roles',(SELECT jsonb_agg(jsonb_build_array(rolname,rolsuper,rolbypassrls,rolcreaterole,
              rolcreatedb,rolinherit) ORDER BY rolname) FROM pg_roles
              WHERE rolname IN ('billing_web','billing_collector'))));""")
        return json.loads(result.stdout)

    def migrate(self):
        result = self.psql(additive.sql_for(PLAN))
        self.assertEqual(json.loads(result.stdout), {'migration': NAME, 'verified': True})

    def assert_rejected_unchanged(self, expected_error):
        before = self.snapshot()
        result = self.psql(additive.sql_for(PLAN), check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(expected_error, result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_first_addition_preserves_original_rows_history_and_access(self):
        connection = self.psql("SELECT json_build_array(current_user,ssl) FROM pg_stat_ssl WHERE pid=pg_backend_pid();")
        self.assertEqual(json.loads(connection.stdout), ['billing_admin', True])
        before = self.snapshot()
        self.migrate()
        after = self.snapshot()
        self.assertEqual([{key: value for key, value in row.items() if key != 'archived_at'}
                          for row in after['rows']], before['rows'])
        self.assertTrue(all(row['archived_at'] is None for row in after['rows']))
        self.assertEqual(after['access'], before['access'])
        self.assertEqual(after['columns'][:-1], before['columns'])
        self.assertEqual(after['columns'][-1], {'name': 'archived_at', 'type': 'timestamp with time zone',
                         'not_null': False, 'default': False, 'identity': '', 'generated': '', 'acl': None})
        self.assertEqual([item for item in after['history'] if item['name'] != NAME], before['history'])
        added = [item for item in after['history'] if item['name'] == NAME]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]['app'], 'billing')

    def test_retry_is_idempotent_after_application_populates_new_field(self):
        self.migrate()
        self.psql("UPDATE public.billing_savedreport SET archived_at='2026-09-22T01:02:03Z' WHERE id=1;")
        before = self.snapshot()
        self.migrate()
        self.assertEqual(self.snapshot(), before)

    def test_changed_grants_or_rls_abort_without_mutation(self):
        for change in ('GRANT UPDATE ON public.billing_savedreport TO billing_collector',
                       'REVOKE DELETE ON public.billing_savedreport FROM billing_web',
                       'ALTER TABLE public.billing_savedreport DISABLE ROW LEVEL SECURITY'):
            with self.subTest(change=change):
                self.setUp()
                self.psql(change)
                self.assert_rejected_unchanged('Existing table ownership or access controls differ')

    def test_changed_or_duplicate_migration_history_aborts_without_mutation(self):
        changes = [
            ("DELETE FROM public.django_migrations WHERE app='billing'", 'Installed database migration history differs'),
            ("INSERT INTO public.django_migrations(app,name,applied) VALUES('billing','0023_unknown',now())",
             'Installed database migration history differs'),
            ("INSERT INTO public.django_migrations(app,name,applied) VALUES('billing','" + NAME + "',now()),"
             "('billing','" + NAME + "',now())", 'Duplicate migration record'),
        ]
        for change, error in changes:
            with self.subTest(change=change):
                self.setUp()
                self.psql(change)
                self.assert_rejected_unchanged(error)

    def test_unrecorded_or_wrong_column_aborts_without_mutation(self):
        cases = [('timestamp with time zone', False, 'An unrecorded target column already exists'),
                 ('text', True, 'Target column does not match nullable DateTimeField'),
                 ('timestamp with time zone NOT NULL DEFAULT now()', True,
                  'Target column does not match nullable DateTimeField')]
        for definition, recorded, error in cases:
            with self.subTest(definition=definition, recorded=recorded):
                self.setUp()
                self.psql('ALTER TABLE public.billing_savedreport ADD COLUMN archived_at ' + definition)
                if recorded:
                    self.psql("INSERT INTO public.django_migrations(app,name,applied) VALUES('billing','" + NAME + "',now())")
                self.assert_rejected_unchanged(error)

    def test_postcondition_failure_rolls_back_column_history_rows_and_grants(self):
        for change in ("UPDATE public.billing_savedreport SET name='Unexpected change' WHERE id=1;",
                       'GRANT UPDATE ON public.billing_savedreport TO billing_collector;'):
            with self.subTest(change=change):
                self.setUp()
                self.psql("""CREATE FUNCTION public.fixture_mutate_after_history() RETURNS trigger LANGUAGE plpgsql AS $$
                  BEGIN
                    IF NEW.app='billing' AND NEW.name='""" + NAME + """' THEN
                      """ + change + """
                    END IF;
                    RETURN NEW;
                  END $$;
                  CREATE TRIGGER fixture_mutation AFTER INSERT ON public.django_migrations
                    FOR EACH ROW EXECUTE FUNCTION public.fixture_mutate_after_history();""")
                self.assert_rejected_unchanged('Existing rows or access controls changed')

    def test_generated_sql_rejects_a_non_tls_connection(self):
        before = self.snapshot()
        result = self.psql(additive.sql_for(PLAN), sslmode='disable', check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Expected the existing TLS administration connection', result.stderr)
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
