"""Adversarial checks for candidate dependency execution and output freezing."""
import os
from pathlib import Path
import stat
# Exception types only; no subprocess executes in this test.
import subprocess  # nosec B404
import sys
import tempfile
import unittest
from unittest.mock import patch

DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DIRECTORY))
import agent

SYNTHETIC_ENV_VALUE = 'isolated-environment-fixture'


class FrozenDependencyOutput(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.venv = self.root / 'venv'
        self.venv.mkdir()

    def freeze(self):
        # Ownership changes require root in production; fixtures stay user-owned.
        with patch.object(agent.os, 'chown'), patch.object(agent.os, 'fchown'):
            agent.freeze_venv(self.venv)

    def test_file_symlink_cannot_modify_its_external_target(self):
        target = self.root / 'outside'
        target.write_text('preserved')
        target.chmod(0o600)
        (self.venv / 'package.py').symlink_to(target)
        with self.assertRaises(ValueError):
            self.freeze()
        self.assertEqual(target.read_text(), 'preserved')
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_directory_symlink_cannot_escape_the_output_tree(self):
        target = self.root / 'outside'
        target.mkdir(mode=0o700)
        protected = target / 'secret'
        protected.write_text('preserved')
        (self.venv / 'lib').symlink_to(target, target_is_directory=True)
        with self.assertRaises(ValueError):
            self.freeze()
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o700)
        self.assertEqual(protected.read_text(), 'preserved')

    def test_dangling_symlinks_and_linked_root_are_rejected(self):
        link = self.venv / 'missing'
        link.symlink_to(self.root / 'not-created')
        with self.assertRaises(ValueError):
            self.freeze()
        link.unlink()
        root_link = self.root / 'linked-venv'
        root_link.symlink_to(self.venv, target_is_directory=True)
        with patch.object(agent.os, 'chown'), patch.object(agent.os, 'fchown'), self.assertRaises(ValueError):
            agent.freeze_venv(root_link)
        self.assertFalse((self.root / 'not-created').exists())

    def test_hardlinks_cannot_change_external_file_permissions(self):
        target = self.root / 'outside'
        target.write_text('preserved')
        target.chmod(0o600)
        os.link(target, self.venv / 'package.py')
        with self.assertRaises(ValueError):
            self.freeze()
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_named_pipe_is_rejected_without_opening_or_blocking(self):
        os.mkfifo(self.venv / 'package.py')
        with self.assertRaises(ValueError):
            self.freeze()

    def test_regular_output_loses_write_and_privileged_mode_bits(self):
        folder = self.venv / 'lib'
        folder.mkdir()
        folder.chmod(0o2777)
        executable = folder / 'python'
        executable.write_text('synthetic executable')
        executable.chmod(0o6777)
        module = folder / 'package.py'
        module.write_text('synthetic module')
        module.chmod(0o666)
        self.freeze()
        self.assertEqual(stat.S_IMODE(self.venv.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(executable.stat().st_mode), 0o755)
        self.assertEqual(stat.S_IMODE(module.stat().st_mode), 0o644)


class DependencyExecutionBoundary(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.executor = agent.Agent({'runtime': 'collector', 'root': str(root / 'runtime'),
                                     'releases': str(root / 'releases')},
                                    'a' * 40 + '-123-1', 'version', 'b' * 64)
        self.source = self.executor.stage / 'source'
        self.source.mkdir()
        (self.source / 'requirements.txt').write_text('synthetic-package==1.0\n')
        (self.executor.stage / 'wheels').mkdir()

    def test_candidate_interpreters_are_always_after_privilege_drop_and_clean_environment(self):
        events = []
        def run(args, **kwargs):
            events.append(('run', args))
            self.assertEqual(kwargs['timeout'], 360)
            self.assertEqual(args[0], 'systemd-run')
            drop = args.index('/usr/bin/setpriv')
            clean = args.index('/usr/bin/env')
            self.assertLess(drop, clean)
            self.assertEqual(args[clean + 1], '-i')
            for option in ('--reuid=65534', '--regid=65534', '--clear-groups',
                           '--inh-caps=-all', '--ambient-caps=-all', '--bounding-set=-all',
                           '--no-new-privs'):
                self.assertIn(option, args[drop:clean])
            for property_value in ('PrivateNetwork=yes', 'PrivateTmp=yes', 'PrivateDevices=yes',
                                   'ProtectSystem=strict', 'ProtectHome=yes', 'ProtectControlGroups=yes',
                                   'NoNewPrivileges=yes', 'RestrictAddressFamilies=AF_UNIX',
                                   'KillMode=control-group'):
                self.assertIn('--property=' + property_value, args[:drop])
            writable = [arg for arg in args if arg.startswith('--property=ReadWritePaths=')]
            self.assertEqual(writable, ['--property=ReadWritePaths=' + str(self.executor.stage / 'venv')])
            self.assertFalse(any('EnvironmentFile=' in arg for arg in args))
            self.assertNotIn(SYNTHETIC_ENV_VALUE, str(args))
            return ''
        with patch.dict(os.environ, {'AWS_SECRET_ACCESS_KEY': SYNTHETIC_ENV_VALUE,
                                     'PYTHONPATH': '/untrusted/host/path'}), \
                patch.object(agent.os, 'chown'), patch.object(agent, 'run', side_effect=run), \
                patch.object(agent, 'stop_builder_unit', side_effect=lambda unit: events.append(('stopped', unit))), \
                patch.object(agent, 'freeze_venv', side_effect=lambda path: events.append(('frozen', path))):
            self.executor.build_collector_venv(self.source)
        self.assertEqual([kind for kind, _ in events], ['run', 'stopped'] * 4 + ['frozen'])
        commands = [args for kind, args in events if kind == 'run']
        self.assertEqual(sum('install' in args and 'pip' in args for args in commands), 1)
        self.assertEqual(sum('check' in args and 'pip' in args for args in commands), 1)
        for args in commands:
            if 'pip' in args:
                self.assertGreater(args.index(str(self.executor.stage / 'venv/bin/python')),
                                   args.index('/usr/bin/env'))

    def test_timeout_stops_the_worker_and_does_not_freeze_partial_output(self):
        with patch.object(agent.os, 'chown'), \
                patch.object(agent, 'run', side_effect=subprocess.TimeoutExpired('builder', 360)), \
                patch.object(agent, 'stop_builder_unit') as stop, \
                patch.object(agent, 'freeze_venv') as freeze:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.executor.build_collector_venv(self.source)
        stop.assert_called_once()
        freeze.assert_not_called()

    def test_unconfirmed_cleanup_blocks_all_later_commands_and_freezing(self):
        with patch.object(agent.os, 'chown'), patch.object(agent, 'run', return_value='') as run, \
                patch.object(agent, 'stop_builder_unit', side_effect=ValueError('cleanup unconfirmed')), \
                patch.object(agent, 'freeze_venv') as freeze:
            with self.assertRaisesRegex(ValueError, 'cleanup unconfirmed'):
                self.executor.build_collector_venv(self.source)
        run.assert_called_once()
        freeze.assert_not_called()


class BuilderCleanupBoundary(unittest.TestCase):
    UNIT = 'billing-build-' + 'a' * 40 + '-123-1-0.service'

    def test_absent_or_inactive_unit_is_required(self):
        for active in ('active', 'activating', 'deactivating'):
            with self.subTest(active=active), patch.object(agent, 'run', side_effect=[
                    '', f'LoadState=loaded\nActiveState={active}\nControlGroup=\n']), \
                    self.assertRaisesRegex(ValueError, 'cleanup is unconfirmed'):
                agent.stop_builder_unit(self.UNIT)

    def test_arbitrary_control_group_is_not_accepted(self):
        with patch.object(agent, 'run', side_effect=[
                '', 'LoadState=loaded\nActiveState=inactive\nControlGroup=/system.slice/other.service\n']), \
                self.assertRaisesRegex(ValueError, 'Unexpected dependency build control group'):
            agent.stop_builder_unit(self.UNIT)

    def test_worker_in_descendant_group_blocks_freezing(self):
        class ProcessList:
            def read_text(self):
                return '12345\n'
        state = f'LoadState=loaded\nActiveState=inactive\nControlGroup=/system.slice/{self.UNIT}\n'
        with patch.object(agent, 'run', side_effect=['', state]), \
                patch.object(agent.Path, 'is_file', return_value=True), \
                patch.object(agent.Path, 'exists', return_value=True), \
                patch.object(agent.Path, 'rglob', return_value=iter([ProcessList()])), \
                self.assertRaisesRegex(ValueError, 'processes are still running'):
            agent.stop_builder_unit(self.UNIT)


if __name__ == '__main__':
    unittest.main()
