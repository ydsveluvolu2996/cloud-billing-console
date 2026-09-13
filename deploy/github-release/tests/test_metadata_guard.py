import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('guard', Path(__file__).resolve().parents[2]/'single-ec2/metadata_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class MetadataRules(unittest.TestCase):
    def state(self):
        rules = guard.rules(995, '169.254.169.254/32')
        # iptables outputs chains alphabetically, not installation order.
        return '\n'.join(['-P FORWARD DROP', '-A FORWARD -j BILLING_CONTAINER_IMDS',
                          '-A FORWARD -j DOCKER-USER', '-A OUTPUT -j BILLING_IMDS',
                          rules[-1]] + rules[:-1])

    def test_kernel_chain_order_does_not_hide_effective_protection(self):
        with patch.object(guard, 'run', return_value=self.state()):
            guard.configure('iptables', 'iptables-restore', '169.254.169.254/32', 995, True)

    def test_accept_before_guard_or_owner_allowlist_change_fails_closed(self):
        for state in [self.state().replace('-A OUTPUT -j BILLING_IMDS', '-A OUTPUT -j ACCEPT\n-A OUTPUT -j BILLING_IMDS'),
                      self.state().replace('--uid-owner 995', '--uid-owner 10001'),
                      self.state().replace('-A FORWARD -j BILLING_CONTAINER_IMDS', ''),
                      self.state().replace('-d 169.254.169.254/32 -j DROP', '-d 169.254.169.254/32 -j ACCEPT')]:
            with self.subTest(state=state), patch.object(guard, 'run', return_value=state):
                with self.assertRaisesRegex(ValueError, 'missing, reordered or changed'):
                    guard.configure('iptables', 'iptables-restore', '169.254.169.254/32', 995, True)

    def test_install_uses_atomic_table_update_preserving_other_rules(self):
        with patch.object(guard, 'run', side_effect=[self.state(), '', self.state()]) as run:
            guard.configure('iptables', 'iptables-restore', '169.254.169.254/32', 995, False)
        call = run.call_args_list[1]
        self.assertIn('--noflush', call.args[0])
        script = call.kwargs['input']
        self.assertNotIn('-F OUTPUT', script)
        self.assertNotIn('-F FORWARD', script)
        self.assertIn('-I OUTPUT 1 -j BILLING_IMDS', script)
        self.assertIn('-I FORWARD 1 -j BILLING_CONTAINER_IMDS', script)
        self.assertTrue(script.endswith('COMMIT\n'))
