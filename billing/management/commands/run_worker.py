"""Supervised collector worker: leases durable jobs, schedules six-hour slots, recovers crashes."""
import signal
import threading
from django.conf import settings
from django.core.management.base import BaseCommand
from billing import jobs, scheduler


class Command(BaseCommand):
    help = 'Run the background worker (bounded concurrency, six-hour scheduling, lease recovery).'

    def add_arguments(self, parser):
        parser.add_argument('--concurrency', type=int, default=None, help='Concurrent jobs (default WORKER_CONCURRENCY).')
        parser.add_argument('--once', action='store_true', help='Schedule due work, run queued jobs until empty, then exit (cron friendly).')
        parser.add_argument('--limit', type=int, default=None, help='With --once: maximum jobs to run.')

    def handle(self, *args, **options):
        if options['once']:
            jobs.recover_expired()
            created = scheduler.schedule_due()
            ran = jobs.run_once(limit=options['limit'])
            self.stdout.write(f'scheduled={created} ran={ran}')
            return
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        concurrency = options['concurrency'] or getattr(settings, 'WORKER_CONCURRENCY', 3)
        self.stdout.write(f'worker starting concurrency={concurrency}')
        jobs.work(concurrency=concurrency, stop_event=stop)
        self.stdout.write('worker stopped')
