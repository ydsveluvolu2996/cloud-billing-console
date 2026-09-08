import fcntl
from pathlib import Path
from django.core.management.base import BaseCommand
from django.utils import timezone
from billing.collector import sync_customer
from billing.models import Customer, SyncRun, SavedReport
from billing.query_cache import refresh_queries


class Command(BaseCommand):
    help = 'Collect connected customer billing; a process lock prevents overlapping runs.'

    def add_arguments(self, parser):
        parser.add_argument('--customer')
        parser.add_argument('--full', action='store_true')
        parser.add_argument('--queued', action='store_true')

    def handle(self, *args, **options):
        with Path('/tmp/cloud-billing-sync.lock').open('w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.stdout.write('Another collection is already running.')
                return
            # Holding the exclusive lock means prior unfinished runs were interrupted.
            for unfinished in SyncRun.objects.filter(status='running'):
                unfinished.status = 'failed'
                unfinished.error = 'Collection was interrupted. A retry has been queued.'
                unfinished.finished_at = timezone.now()
                unfinished.save()
                Customer.objects.filter(pk=unfinished.customer_id).update(sync_requested=True, last_error=unfinished.error)
            customers = Customer.objects.filter(enabled=True).exclude(role_arn='')
            if options['customer']:
                customers = customers.filter(pk=options['customer'])
            if options['queued']:
                customers = customers.filter(sync_requested=True)
            for customer in customers:
                Customer.objects.filter(pk=customer.pk).update(sync_requested=False)
                full = options['full'] or (timezone.now().day == 2 and timezone.now().hour < 6)
                run = sync_customer(customer, full=full)
                self.stdout.write(f'{customer.name}: {run.status if run else "skipped"}')

            if not options['queued']:
                from billing.advanced_explorer import build_report
                for saved in SavedReport.objects.all():
                    try:
                        build_report(saved.parameters)
                    except ValueError:
                        self.stderr.write(f'Saved report {saved.pk} needs parameter review.')
            refresh_queries(customer_id=options['customer'], scheduled=not options['queued'])
