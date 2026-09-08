"""Isolated synthetic load test: 100 customers, 2,000+ linked accounts, realistic history.

Refuses to run against a database that already holds real customers unless --allow-existing-data
is passed, and never touches AWS: collection is simulated with an in-process fake Cost Explorer
client that returns paginated pages. Results are written as JSON for the capacity document.
"""
import json
import random
import statistics
import threading
import time
import tracemalloc
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from django.conf import settings
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, connections, reset_queries
from django.test import Client
from django.test.utils import CaptureQueriesContext, override_settings
from django.utils import timezone
from billing import budgets, jobs, scheduler
from billing.aws import Meter
from billing.collector import collect_source
from billing.models import (AccountAssignment, AwsAccount, BillingSource, Budget, BudgetAmount, CollectionPeriod, Cost, Customer, Job)

SERVICES = ['Amazon Elastic Compute Cloud - Compute', 'Amazon Simple Storage Service', 'Amazon Relational Database Service',
            'AmazonCloudWatch', 'AWS Support (Business)', 'Amazon Virtual Private Cloud', 'EC2 - Other', 'AWS Lambda']


class FakeCostExplorer:
    """Deterministic paginated GetCostAndUsage responses for a payer's accounts."""

    def __init__(self, accounts, services, page_size=500, seed=0, latency=0.0):
        self.accounts, self.services, self.page_size, self.latency = accounts, services, page_size, latency
        self.random = random.Random(seed)
        self.calls = 0

    def get_cost_and_usage(self, **params):
        self.calls += 1
        if self.latency:
            time.sleep(self.latency)
        start = date.fromisoformat(params['TimePeriod']['Start'])
        end = date.fromisoformat(params['TimePeriod']['End'])
        page = int(params.get('NextPageToken', '0'))
        days = [start + timedelta(days=i) for i in range((end - start).days)]
        rows = [(d, a, s) for d in days for a in self.accounts for s in self.services]
        chunk = rows[page * self.page_size:(page + 1) * self.page_size]
        by_day = {}
        for d, a, s in chunk:
            amount = Decimal(str(round(self.random.uniform(0.5, 40.0), 10)))
            if self.random.random() < 0.02:
                amount = -amount / 4  # credits
            by_day.setdefault(d, []).append({'Keys': [a, s], 'Metrics': {'UnblendedCost': {'Amount': str(amount), 'Unit': 'USD'}, 'AmortizedCost': {'Amount': str(amount), 'Unit': 'USD'}}})
        result = {'ResultsByTime': [{'TimePeriod': {'Start': str(d), 'End': str(d + timedelta(days=1))}, 'Estimated': d >= date.today().replace(day=1), 'Groups': groups} for d, groups in by_day.items()]}
        if (page + 1) * self.page_size < len(rows):
            result['NextPageToken'] = str(page + 1)
        return result


class Command(BaseCommand):
    help = 'Generate synthetic customers/accounts/costs and measure dashboard and collection performance.'

    def add_arguments(self, parser):
        parser.add_argument('--customers', type=int, default=100)
        parser.add_argument('--accounts', type=int, default=2000)
        parser.add_argument('--months', type=int, default=7)
        parser.add_argument('--services', type=int, default=6)
        parser.add_argument('--readers', type=int, default=8, help='Concurrent simulated dashboard readers')
        parser.add_argument('--requests', type=int, default=160, help='Total dashboard requests across readers')
        parser.add_argument('--collect-sources', type=int, default=100, help='Sources to run through a simulated collection cycle')
        parser.add_argument('--output', default='docs/capacity/synthetic-load-results.json')
        parser.add_argument('--allow-existing-data', action='store_true')
        parser.add_argument('--keep', action='store_true', help='Keep generated data (default deletes it afterwards)')
        parser.add_argument('--seed', type=int, default=42)

    def handle(self, *args, **options):
        if not settings.DEBUG and not options['allow_existing_data']:
            raise CommandError('Refusing to run outside DEBUG without --allow-existing-data; never run this against production.')
        if Customer.objects.exclude(name__startswith='Synthetic ').exists() and not options['allow_existing_data']:
            raise CommandError('The database already contains non-synthetic customers. Use an isolated database.')
        random.seed(options['seed'])
        results = {'generated_at': timezone.now().isoformat(), 'parameters': {k: options[k] for k in ('customers', 'accounts', 'months', 'services', 'readers', 'requests', 'collect_sources')},
                   'database': connection.vendor, 'hardware_note': 'Fill in from the host running this command (CPU, RAM, disk).'}
        try:
            results['generation'] = self.generate(options)
            results['collection_cycle'] = self.simulate_collection(options)
            results['budgets'] = self.evaluate_budgets()
            results['dashboard'] = self.measure_dashboard(options)
            results['queue_fairness'] = self.measure_fairness(options)
            results['table_sizes'] = self.table_sizes()
        finally:
            if not options['keep']:
                self.cleanup()
        path = Path(options['output'])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(results, indent=2, default=str))
        self.stdout.write(json.dumps(results, indent=2, default=str))

    # --- data generation ---------------------------------------------------------------------
    def generate(self, options):
        started = time.monotonic()
        tracemalloc.start()
        today = timezone.now().date()
        first_month = today.replace(day=1)
        for _ in range(options['months'] - 1):
            first_month = (first_month - timedelta(days=1)).replace(day=1)
        days = [first_month + timedelta(days=i) for i in range((today - first_month).days + 1)]
        services = SERVICES[:options['services']]
        per_customer = max(1, options['accounts'] // options['customers'])
        account_counter = 100000000000
        total_rows = 0
        for i in range(options['customers']):
            customer = Customer.objects.create(name=f'Synthetic {i:03d}', reference=f'SYN-{i:03d}', owner='load test')
            payer_id = f'{account_counter:012d}'
            account_counter += 1
            source = BillingSource.objects.create(customer=customer, kind='payer', account_id=payer_id, role_arn=f'arn:aws:iam::{payer_id}:role/BillingConsole/CostReadOnly',
                                                  verified_at=timezone.now(), discovered_at=timezone.now(), initial_import_done=True, last_success=timezone.now(),
                                                  discovery_mode='organizations', onboarding_step=6, capabilities={'organizations': True, 'cost_explorer': True})
            accounts = [payer_id]
            for _ in range(per_customer - 1):
                accounts.append(f'{account_counter:012d}')
                account_counter += 1
            aws_accounts = [AwsAccount(account_id=a, name=f'acct-{a[-4:]}', state='ACTIVE', payer_account_id=payer_id, source=source, discovery='organizations') for a in accounts]
            AwsAccount.objects.bulk_create(aws_accounts)
            AccountAssignment.objects.bulk_create([AccountAssignment(account=a, customer=customer, start=date(2000, 1, 1)) for a in aws_accounts])
            budget = Budget.objects.create(customer=customer, scope=Budget.CUSTOMER, name=f'{customer.name} monthly', created_by='load test')
            BudgetAmount.objects.create(budget=budget, amount=Decimal(random.randint(2000, 50000)))
            rows = []
            for day in days:
                estimated = day >= today.replace(day=1)
                for account in accounts:
                    scale = random.uniform(0.2, 3.0)
                    for service in services:
                        amount = Decimal(str(round(random.uniform(0.1, 25.0) * scale, 6)))
                        rows.append(Cost(source=source, customer=customer, day=day, account_id=account, service=service, currency='USD', unblended=amount, amortized=amount, estimated=estimated))
                if len(rows) >= 5000:
                    Cost.objects.bulk_create(rows, batch_size=5000)
                    total_rows += len(rows)
                    rows = []
            if rows:
                Cost.objects.bulk_create(rows, batch_size=5000)
                total_rows += len(rows)
            month = first_month
            while month <= today:
                CollectionPeriod.objects.create(source=source, month=month, status='complete', revision=1, last_success=timezone.now(), rows=len(accounts) * len(services) * 28)
                month = (month + timedelta(days=32)).replace(day=1)
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return {'seconds': round(time.monotonic() - started, 1), 'cost_rows': total_rows, 'accounts': AwsAccount.objects.count(), 'customers': options['customers'],
                'days': len(days), 'python_peak_memory_mb': round(peak / 1024 / 1024, 1)}

    # --- simulated collection ----------------------------------------------------------------
    def simulate_collection(self, options):
        """Run the real collector against a fake paginated Cost Explorer for N sources."""
        today = timezone.now().date()
        months = [today.replace(day=1) - timedelta(days=1)]
        months = [months[0].replace(day=1), today.replace(day=1)]
        sources = list(BillingSource.objects.filter(customer__name__startswith='Synthetic ').select_related('customer')[:options['collect_sources']])
        durations, requests, pages_total, rows_total = [], [], 0, 0
        started = time.monotonic()
        tracemalloc.start()
        for source in sources:
            accounts = list(AwsAccount.objects.filter(source=source).values_list('account_id', flat=True))
            fake = FakeCostExplorer(accounts, SERVICES[:options['services']], page_size=1000, seed=hash(source.account_id) % 1000)
            meter = Meter(limit=0)
            t0 = time.monotonic()
            run = collect_source(source, months=months, client=fake, meter=meter, today=today)
            durations.append(time.monotonic() - t0)
            requests.append(meter.requests)
            pages_total += meter.pages
            rows_total += run.rows
        current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        total = time.monotonic() - started
        per_request_usd = 0.01
        cycle_requests = sum(requests)
        return {'sources': len(sources), 'months_per_source': len(months), 'wall_seconds': round(total, 1),
                'per_source_seconds': {'mean': round(statistics.mean(durations), 2), 'p95': round(percentile(durations, 95), 2), 'max': round(max(durations), 2)},
                'requests_per_source': {'mean': round(statistics.mean(requests), 1), 'max': max(requests)},
                'rows_published': rows_total, 'pages': pages_total, 'python_peak_memory_mb': round(peak / 1024 / 1024, 1),
                'estimated_ce_cost_usd_per_cycle_at_0_01': round(cycle_requests * per_request_usd, 2),
                'estimated_ce_cost_usd_per_day_4_cycles': round(cycle_requests * per_request_usd * 4, 2),
                'projected_full_cycle_seconds_single_worker': round(total / max(len(sources), 1) * BillingSource.objects.filter(customer__name__startswith='Synthetic ').count(), 1),
                'note': 'Fake client has zero network latency; live AWS adds roughly 0.5-2 s per request and throttles at a few requests per second per account.'}

    def evaluate_budgets(self):
        started = time.monotonic()
        with override_settings(BUDGET_AWS_FORECASTS=False):
            count = budgets.evaluate_all()
        return {'evaluated': count, 'seconds': round(time.monotonic() - started, 1)}

    # --- dashboard measurements ----------------------------------------------------------------
    def measure_dashboard(self, options):
        user, _ = User.objects.get_or_create(username='synthetic-reader', defaults={'is_staff': True})
        customers = list(Customer.objects.filter(name__startswith='Synthetic ').values_list('pk', flat=True))
        today = timezone.now().date()
        start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        paths = ['/', '/portfolio/', '/overview/', '/customers/', '/budgets/', '/onboarding/',
                 f'/portfolio/?start={start}&end={today}&granularity=monthly', '/?date_range=last_3_months&group_by=account']
        paths += [f'/customers/{pk}/' for pk in customers[:10]] + [f'/customers/{pk}/?tab=budgets' for pk in customers[:5]] + [f'/?customer={pk}' for pk in customers[:10]]
        per_path = {}
        with override_settings(SECURE_SSL_REDIRECT=False, ALLOWED_HOSTS=['testserver'], DEBUG=True, STORAGES={'default': {'BACKEND': 'django.core.files.storage.FileSystemStorage'}, 'staticfiles': {'BACKEND': 'django.contrib.staticfiles.storage.StaticFilesStorage'}}):
            client = Client()
            client.force_login(user)
            connection.force_debug_cursor = True
            for path in paths:
                connection.queries_log.clear()
                t0 = time.monotonic()
                try:
                    response = client.get(path)
                    status = response.status_code
                except Exception as exc:  # record the failure instead of aborting the whole run
                    status = f'error: {type(exc).__name__}'
                elapsed = time.monotonic() - t0
                queries = list(connection.queries_log)
                for other in connections.all():
                    if other is not connection and other.alias != connection.alias:
                        queries.extend(other.queries_log)
                key = path.split('?')[0] if 'customers/' not in path else '/customers/<id>/' + ('?tab=budgets' if 'tab=budgets' in path else '')
                per_path.setdefault(key, {'samples': [], 'queries': [], 'status': status})
                per_path[key]['samples'].append(elapsed)
                per_path[key]['queries'].append(len(queries))
            connection.force_debug_cursor = False
            summary = {k: {'status': v['status'], 'requests': len(v['samples']), 'p50_ms': round(percentile(v['samples'], 50) * 1000), 'p95_ms': round(percentile(v['samples'], 95) * 1000),
                           'queries_mean': round(statistics.mean(v['queries']), 1), 'queries_max': max(v['queries'])} for k, v in per_path.items()}
            # concurrent readers: threads with their own clients hitting a mix of pages
            latencies = []
            lock = threading.Lock()
            mix = ['/', '/portfolio/', '/overview/', '/customers/', '/budgets/'] + [f'/customers/{pk}/' for pk in customers[:20]]
            per_reader = max(1, options['requests'] // options['readers'])

            def reader(index):
                from django.db import close_old_connections
                c = Client()
                c.force_login(user)
                rng = random.Random(index)
                local = []
                for _ in range(per_reader):
                    t0 = time.monotonic()
                    try:
                        c.get(rng.choice(mix))
                    except Exception:
                        pass
                    local.append(time.monotonic() - t0)
                close_old_connections()
                with lock:
                    latencies.extend(local)
            tracemalloc.start()
            t0 = time.monotonic()
            threads = [threading.Thread(target=reader, args=(i,)) for i in range(options['readers'])]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            wall = time.monotonic() - t0
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        return {'per_path': summary, 'concurrent': {'readers': options['readers'], 'requests': len(latencies), 'wall_seconds': round(wall, 1),
                                                     'throughput_rps': round(len(latencies) / wall, 1), 'p50_ms': round(percentile(latencies, 50) * 1000),
                                                     'p95_ms': round(percentile(latencies, 95) * 1000), 'max_ms': round(max(latencies) * 1000),
                                                     'python_peak_memory_mb': round(peak / 1024 / 1024, 1)},
                'note': 'Django test client in-process (no gunicorn/Caddy/TLS); concurrency limited by the GIL and one PostgreSQL connection per thread.'}

    def measure_fairness(self, options):
        """Schedule one slot for every source and check the lease order distributes across sources."""
        Job.objects.all().delete()
        now = timezone.now()
        created = scheduler.schedule_due(now)
        leased_sources, order = [], []
        started = time.monotonic()
        while True:
            job = jobs.lease('fairness-probe', now=now + timedelta(hours=1))
            if not job:
                break
            order.append(job.kind)
            if job.source_id:
                leased_sources.append(str(job.source_id))
            jobs.complete(job)
        collect_positions = [i for i, kind in enumerate(order) if kind == 'collect']
        return {'jobs_scheduled': created, 'lease_loop_seconds': round(time.monotonic() - started, 2), 'distinct_sources_leased': len(set(leased_sources)),
                'collect_jobs': len(collect_positions), 'first_collect_position': collect_positions[0] if collect_positions else None,
                'lease_ms_per_job': round((time.monotonic() - started) / max(len(order), 1) * 1000, 2),
                'note': 'Per-source exclusion means no two jobs of one connection run at once; run_after jitter spreads the slot across SCHEDULE_JITTER_SECONDS.'}

    def table_sizes(self):
        if connection.vendor != 'postgresql':
            return {'note': 'table sizes available on PostgreSQL only'}
        with connection.cursor() as cursor:
            cursor.execute("select relname, pg_size_pretty(pg_total_relation_size(relid)) from pg_catalog.pg_statio_user_tables where relname like 'billing_%' order by pg_total_relation_size(relid) desc limit 8")
            return dict(cursor.fetchall())

    def cleanup(self):
        synthetic = Customer.objects.filter(name__startswith='Synthetic ')
        Job.objects.all().delete()
        Cost.objects.filter(customer__in=synthetic).delete()
        from billing.models import BudgetEvaluation, Alert, ExplorerQuery
        Alert.objects.filter(budget__customer__in=synthetic).delete()
        BudgetEvaluation.objects.filter(budget__customer__in=synthetic).delete()
        BudgetAmount.objects.filter(budget__customer__in=synthetic).delete()
        Budget.objects.filter(customer__in=synthetic).delete()
        ExplorerQuery.objects.filter(source__customer__in=synthetic).delete()
        CollectionPeriod.objects.filter(source__customer__in=synthetic).delete()
        AccountAssignment.objects.filter(customer__in=synthetic).delete()
        AwsAccount.objects.filter(source__customer__in=synthetic).delete()
        from billing.models import SyncRun, AuditEvent
        SyncRun.objects.filter(source__customer__in=synthetic).delete()
        AuditEvent.objects.filter(customer__in=synthetic).delete()
        BillingSource.objects.filter(customer__in=synthetic).delete()
        synthetic.delete()
        User.objects.filter(username='synthetic-reader').delete()


def percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(pct / 100 * (len(ordered) - 1))))
    return ordered[index]
