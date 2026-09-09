"""Compatibility entry point for the legacy cron lines.

Collection now runs through the durable job queue. This command schedules the current slot,
honours manual refresh requests and runs queued jobs once, sharing the same process lock as
before so overlapping cron invocations never double-collect.
"""
import fcntl
from pathlib import Path
from django.core.management.base import BaseCommand, CommandError
from django.conf import settings
import os
from billing import jobs, scheduler
from billing.models import BillingSource, SavedReport


class Command(BaseCommand):
    help = 'Schedule and run queued collection jobs once; a process lock prevents overlapping runs.'

    def add_arguments(self, parser):
        parser.add_argument('--customer')
        parser.add_argument('--full', action='store_true')
        parser.add_argument('--queued', action='store_true')

    def handle(self, *args, **options):
        if settings.RUNTIME_ROLE != 'collector':
            raise CommandError('Collection runs only in the isolated collector runtime.')
        directory = settings.BASE_DIR / '.deployment' if settings.DEBUG else Path('/run/cloud-billing')
        directory.mkdir(mode=0o700,exist_ok=True)
        descriptor=os.open(directory/'sync.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        with os.fdopen(descriptor,'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.stdout.write('Another collection is already running.')
                return
            jobs.recover_expired()
            sources = scheduler.active_sources()
            if options['customer']:
                sources = sources.filter(customer_id=options['customer'])
            if options['full'] or options['customer']:
                for source in sources:
                    scheduler.request_refresh(source, months_back=6 if options['full'] else 1)
            if not options['queued']:
                scheduler.schedule_due()
                from billing.advanced_explorer import build_report
                for saved in SavedReport.objects.all():
                    try:
                        build_report(saved.parameters)
                    except ValueError:
                        self.stderr.write(f'Saved report {saved.pk} needs parameter review.')
            ran = jobs.run_once()
            self.stdout.write(f'jobs run: {ran}')
