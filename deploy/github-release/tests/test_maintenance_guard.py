import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import agent


class MaintenanceGuard(unittest.TestCase):
    def test_release_blocks_uncertain_or_active_database_maintenance(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = object.__new__(agent.Agent)
            executor.root = Path(directory)
            folder = executor.root / '.deployment'
            folder.mkdir()
            marker = folder / 'postgres-maintenance.json'
            executor.require_no_database_maintenance()
            for phase in ('frozen', 'switching', 'awaiting-verification', 'rolled-back', 'unexpected', None):
                marker.write_text(json.dumps({'phase': phase}))
                with self.assertRaises(ValueError):
                    executor.require_no_database_maintenance()
            for phase in ('rehearsed', 'writers-reopened'):
                marker.write_text(json.dumps({'phase': phase}))
                executor.require_no_database_maintenance()
            marker.write_text('incomplete')
            with self.assertRaises(ValueError):
                executor.require_no_database_maintenance()

    def test_application_rollback_cannot_cross_database_cutover(self):
        with tempfile.TemporaryDirectory() as directory:
            executor = object.__new__(agent.Agent)
            executor.root = Path(directory)
            backup = executor.root / 'backup'
            backup.mkdir()
            old = 'BILLING_APP_IMAGE=old\nBILLING_POSTGRES_IMAGE=pg17\n'
            (backup / 'environment').write_text(old)
            (executor.root / '.env').write_text(old.replace('APP_IMAGE=old', 'APP_IMAGE=new'))
            executor.require_database_rollback_compatible(backup)
            (executor.root / '.env').write_text('BILLING_POSTGRES_IMAGE=pg18\nBILLING_POSTGRES_VOLUME=new-volume\n')
            with self.assertRaisesRegex(ValueError, 'maintenance recovery'):
                executor.require_database_rollback_compatible(backup)
