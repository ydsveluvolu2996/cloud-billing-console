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
