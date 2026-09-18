"""Root-supervised local coordinator; never run in either ordinary runtime."""
import fcntl
import os
import time
from pathlib import Path
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import close_old_connections, connection
from billing.activation import ACTIVE_STATUSES, process_activation
from billing.models import ActivationRequest


class Command(BaseCommand):
    help = 'Process administrator-submitted connection requests through the constrained onboarding broker.'

    def add_arguments(self, parser):
        parser.add_argument('--once', action='store_true')
        parser.add_argument('--limit', type=int, default=20)
        parser.add_argument('--interval', type=int, default=15)

    def handle(self, *args, **options):
        if settings.RUNTIME_ROLE != 'admin':
            raise CommandError('The activation coordinator requires the isolated administration runtime.')
        if options['limit'] < 1 or not 5 <= options['interval'] <= 300:
            raise CommandError('Use a positive limit and an interval between 5 and 300 seconds.')
        if connection.vendor == 'postgresql':
            with connection.cursor() as cursor:
                cursor.execute('SELECT current_user')
                if cursor.fetchone()[0] in ('billing_web', 'billing_collector'):
                    raise CommandError('A runtime database identity cannot activate customer connections.')
        lock = Path(getattr(settings, 'ONBOARDING_LOCK_FILE', '/run/cloud-billing-activation/lock'))
        lock.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(lock, os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CommandError('Another activation coordinator is running.') from None
            while True:
                close_old_connections()
                requests = ActivationRequest.objects.filter(status__in=ACTIVE_STATUSES).order_by('created_at')[:options['limit']]
                for request in requests:
                    process_activation(request)
                if options['once']:
                    self.stdout.write('Activation requests processed.')
                    return
                time.sleep(options['interval'])
        finally:
            os.close(descriptor)
