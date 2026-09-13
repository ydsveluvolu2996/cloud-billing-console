"""Exercise the catalog query against each real PostgreSQL CI service."""
import importlib.util
import json
import os
from pathlib import Path
import unittest

spec = importlib.util.spec_from_file_location('maintenance_catalog', Path(__file__).resolve().parents[1] / 'upgrade.py')
maintenance = importlib.util.module_from_spec(spec)
spec.loader.exec_module(maintenance)


@unittest.skipUnless(os.environ.get('DB_HOST'), 'Requires the isolated PostgreSQL CI database')
class CatalogCompatibility(unittest.TestCase):
    def test_catalog_query_and_required_security_fields(self):
        import psycopg
        with psycopg.connect(host=os.environ['DB_HOST'], dbname='billing', user='billing',
                            password=os.environ['DB_PASSWORD']) as conn:
            with conn.cursor() as cursor:
                cursor.execute(maintenance.CATALOG)
                snapshot = json.loads(cursor.fetchone()[0])
                self.assertEqual(set(snapshot), {'roles', 'schemas', 'database', 'memberships',
                                                'relations', 'policies', 'functions', 'default_acl', 'extensions'})
                self.assertTrue(snapshot['roles'])
                self.assertIn('rolpassword', snapshot['roles'][0])
                self.assertIn('rolconfig', snapshot['roles'][0])
