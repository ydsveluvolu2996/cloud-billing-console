"""Prevent a web runtime upgrade from silently changing native wheel compatibility."""
from pathlib import Path
import unittest
import yaml

ROOT = Path(__file__).resolve().parents[3]


class RuntimeMatrix(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.jobs = yaml.safe_load((ROOT / '.github/workflows/checks.yml').read_text())['jobs']

    def test_both_python_runtimes_gate_production(self):
        test = self.jobs['test']
        self.assertEqual(set(test['strategy']['matrix']['python']), {'3.12', '3.14'})
        self.assertFalse(test['strategy']['fail-fast'])
        self.assertNotIn('continue-on-error', test)
        setup = next(step for step in test['steps'] if step.get('uses', '').startswith('actions/setup-python@'))
        self.assertEqual(setup['with']['python-version'], '${{ matrix.python }}')
        commands = '\n'.join(step.get('run', '') for step in test['steps'])
        for expected in ('python -m pip check', 'manage.py test billing.tests', 'manage.py migrate', 'manage.py run_worker', 'manage.py security_load'):
            self.assertIn(expected, commands)
        self.assertEqual(set(self.jobs['deploy']['needs']), {'test', 'security'})
        self.assertIn('inputs.deploy', self.jobs['deploy']['if'])
        self.assertEqual(self.jobs['deploy']['env']['EXPECTED_SHA'], '${{ inputs.expected_sha }}')

    def test_native_collector_packaging_stays_python_312(self):
        security = self.jobs['security']
        setup = next(step for step in security['steps'] if step.get('uses', '').startswith('actions/setup-python@'))
        self.assertEqual(setup['with']['python-version'], '3.12')
        commands = '\n'.join(step.get('run', '') for step in security['steps'])
        self.assertIn('python deploy/github-release/package.py', commands)
        self.assertNotIn('matrix', security)
        self.assertIn('manage.py test billing.tests', commands)

    def test_matrix_evidence_cannot_overwrite_other_runtime(self):
        upload = next(step for step in self.jobs['test']['steps'] if step.get('uses', '').startswith('actions/upload-artifact@'))
        self.assertIn('${{ matrix.python }}', upload['with']['name'])
        self.assertIn('${{ github.sha }}', upload['with']['name'])
