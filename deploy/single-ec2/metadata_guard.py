#!/usr/bin/python3
"""Keep EC2 credentials available to root/collector only, never forwarded containers."""
import argparse
import os
import pwd
import subprocess


def run(args, **kwargs):
    return subprocess.run(args, check=True, capture_output=True, text=True, **kwargs).stdout


def rules(uid, address):
    return [
        '-A BILLING_IMDS -d ' + address + ' -m owner --uid-owner 0 -j RETURN',
        '-A BILLING_IMDS -d ' + address + ' -m owner --uid-owner ' + str(uid) + ' -j RETURN',
        '-A BILLING_IMDS -d ' + address + ' -j DROP',
        '-A BILLING_CONTAINER_IMDS -d ' + address + ' -j DROP',
    ]


def configure(binary, restore, address, uid, check):
    current = run([binary, '-w', '5', '-S']).splitlines()
    expected = rules(uid, address)
    jumps = ['-A OUTPUT -j BILLING_IMDS', '-A FORWARD -j BILLING_CONTAINER_IMDS']
    first = {chain: next((line for line in current if line.startswith('-A ' + chain + ' ')), '')
             for chain in ('OUTPUT', 'FORWARD')}
    chains_match = all([line for line in current if line.startswith('-A ' + chain + ' ')] ==
                       [line for line in expected if line.startswith('-A ' + chain + ' ')]
                       for chain in ('BILLING_IMDS', 'BILLING_CONTAINER_IMDS'))
    if check:
        if not chains_match or any(first[chain] != jump for chain, jump in zip(('OUTPUT', 'FORWARD'), jumps)):
            raise ValueError('Metadata firewall is missing, reordered or changed: ' + binary)
        return
    script = ['*filter', ':BILLING_IMDS - [0:0]', ':BILLING_CONTAINER_IMDS - [0:0]',
              '-F BILLING_IMDS', '-F BILLING_CONTAINER_IMDS']
    for jump in jumps:
        script += [jump.replace('-A ', '-D ', 1)] * current.count(jump)
    script += expected + ['-I OUTPUT 1 -j BILLING_IMDS', '-I FORWARD 1 -j BILLING_CONTAINER_IMDS', 'COMMIT']
    run([restore, '--wait', '5', '--noflush'], input='\n'.join(script) + '\n')
    configure(binary, restore, address, uid, True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise ValueError('Metadata firewall requires root')
    uid = pwd.getpwnam('billing-collector').pw_uid
    configure('/usr/sbin/iptables', '/usr/sbin/iptables-restore', '169.254.169.254/32', uid, args.check)
    configure('/usr/sbin/ip6tables', '/usr/sbin/ip6tables-restore', 'fd00:ec2::254/128', uid, args.check)
    print('Metadata firewall verified for host users and forwarded containers (IPv4/IPv6).')


if __name__ == '__main__':
    main()
