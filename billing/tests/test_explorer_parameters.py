from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import Mock, patch
from django.test import TestCase, Client
from django.contrib.auth import get_user_model
from django.utils import timezone
from botocore.exceptions import ClientError
from billing.parameters import normalize, expression, aws_request, import_console_url, FILTERS, querydict
from billing.advanced_explorer import build_report
from billing.query_cache import get_query, run_query, fetch_pages
from billing.models import Customer, Cost, ExplorerQuery, SavedReport


class ExplorerParameterTests(TestCase):
    def setUp(self):
        self.today=timezone.now().date();self.start=self.today.replace(day=1)
        self.customer=Customer.objects.create(name='One',account_id='111111111111',role_arn='arn:aws:iam::111111111111:role/BillingConsole/CostReadOnly',last_success=timezone.now())
        self.user=get_user_model().objects.create_user('operator',password='test-pass-long',is_staff=True)
        self.client.force_login(self.user)
        self.params={'start':str(self.start),'end':str(self.today),'group_by':'region','forecast':'0'}

    def response(self,value='5.25',unit='USD'):
        return {'ResultsByTime':[{'TimePeriod':{'Start':str(self.start),'End':str(self.today+timedelta(days=1))},'Estimated':True,'Groups':[{'Keys':['ap-south-1'],'Metrics':{'UnblendedCost':{'Amount':value,'Unit':unit}}}]}]}

    def complete(self,queries):
        for q in queries:
            client=Mock();client.get_cost_and_usage.return_value=self.response();run_query(q,client)

    def test_combined_filters_exclusion_absence_and_zero_values(self):
        p=normalize(self.params|{'service':['EC2','S3'],'region':['ap-south-1','us-east-1'],'charge_type':['Credit'],'charge_type_mode':'exclude','tag_key':'Team','untagged':'1'})
        exp=expression(p)
        self.assertEqual(len(exp['And']),4)
        self.assertIn({'Not':{'Dimensions':{'Key':'RECORD_TYPE','Values':['Credit']}}},exp['And'])
        self.assertIn({'Tags':{'Key':'Team','MatchOptions':['ABSENT']}},exp['And'])
        self.assertEqual(normalize(self.params|{'tag':['0'],'tag_key':'CostCenter'})['tag'],['0'])
        self.assertEqual(normalize(querydict(p)),p)

    def test_all_filter_dimensions_and_cost_metrics(self):
        for k in FILTERS:
            extras={k:['example']}
            if k in ('tag','cost_category'):extras[k+'_key']='Team'
            if k=='resource':extras.update(service=['Amazon Elastic Compute Cloud - Compute'],start=str(self.today),end=str(self.today))
            p=normalize(self.params|extras)
            self.assertIsNotNone(expression(p))
        for k,v in [('unblended','UnblendedCost'),('amortized','AmortizedCost'),('blended','BlendedCost'),('net_unblended','NetUnblendedCost'),('net_amortized','NetAmortizedCost')]:
            self.assertEqual(aws_request(normalize(self.params|{'metric':k}))[1]['Metrics'],[v])

    def test_invalid_usage_resource_dates_and_group_are_rejected(self):
        for extra in [{'measure':'usage'},{'group_by':'nonsense'},{'untagged':'1'},{'group_by':'tag'},{'group_by':'resource'},{'start':'2000-01-01'},{'tag_key':'Team','tag':['yes'],'untagged':'1'},{'normalized':'1'}]:
            with self.subTest(extra=extra),self.assertRaises(ValueError):normalize(self.params|extra)
        p=normalize(self.params|{'measure':'usage','usage_type':['USE1-BoxUsage:t3.small'],'normalized':'1'})
        self.assertEqual(aws_request(p)[1]['Metrics'],['NormalizedUsageAmount'])

    def test_cache_queues_once_and_does_not_call_aws_from_web(self):
        with patch('billing.query_cache.cost_client') as aws:
            first=build_report(self.params);second=build_report(self.params|{'chart_style':'line'})
        aws.assert_not_called();self.assertEqual(first['query_ids'],second['query_ids']);self.assertEqual(ExplorerQuery.objects.count(),1)
        self.assertTrue(first['report_incomplete']);self.assertTrue(first['report_pending'])
        self.complete(ExplorerQuery.objects.all())
        report=build_report(self.params)
        self.assertEqual(report['total'],Decimal('5.25'));self.assertFalse(report['report_pending']);self.assertFalse(report['report_incomplete'])
        self.assertEqual(report['period_totals'],[Decimal('5.25')])
        result=self.client.get('/export/report/',self.params)
        self.assertContains(result,'5.25');self.assertContains(result,'Snapshot')

    def test_failed_refresh_retains_data_and_cooldown(self):
        p=normalize(self.params);op,req=aws_request(p);q=get_query(self.customer,op,req)
        self.complete([q]);q.refresh_from_db()
        ce=Mock();ce.get_cost_and_usage.side_effect=ClientError({'Error':{'Code':'AccessDeniedException','Message':'sensitive raw detail'}},'GetCostAndUsage')
        run_query(q,ce);q.refresh_from_db()
        self.assertIsNotNone(q.data);self.assertIn('permission missing',q.error);self.assertNotIn('sensitive',q.error)
        self.assertFalse(get_query(self.customer,op,req).requested)
        self.assertEqual(build_report(self.params)['total'],Decimal('5.25'))

    def test_role_rotation_does_not_reuse_cache(self):
        p=normalize(self.params);op,req=aws_request(p);first=get_query(self.customer,op,req)
        import uuid
        self.customer.external_id=uuid.uuid4();self.customer.save()
        second=get_query(self.customer,op,req)
        self.assertNotEqual(first.pk,second.pk)
        ce=Mock();run_query(first,ce);ce.get_cost_and_usage.assert_not_called()

    def test_pagination_keeps_period_fragments_and_credits(self):
        ce=Mock();page=self.response('10');page['NextPageToken']='next';ce.get_cost_and_usage.side_effect=[page,self.response('-1.5')]
        p=normalize(self.params);op,req=aws_request(p);q=get_query(self.customer,op,req);run_query(q,ce)
        self.assertEqual(build_report(self.params)['total'],Decimal('8.5'))
        self.assertEqual(ce.get_cost_and_usage.call_args_list[1].kwargs['NextPageToken'],'next')
        ce.get_cost_and_usage.side_effect=[page,page]
        with self.assertRaises(ValueError):fetch_pages(ce,op,req)

    def test_partial_customer_reports_block_csv_and_mixed_units_rejected(self):
        Customer.objects.create(name='Two',account_id='222222222222',role_arn='arn:aws:iam::222222222222:role/BillingConsole/CostReadOnly',last_success=timezone.now())
        build_report(self.params);self.complete(ExplorerQuery.objects.filter(customer=self.customer))
        self.assertTrue(build_report(self.params)['report_incomplete']);self.assertEqual(self.client.get('/export/report/',self.params).status_code,409)
        q=ExplorerQuery.objects.exclude(customer=self.customer).get();q.data=self.response(unit='EUR');q.requested=False;q.save()
        with self.assertRaises(ValueError):build_report(self.params)

    def test_compare_uses_calendar_months_and_same_filters(self):
        p=normalize({'start':'2026-07-01','end':'2026-08-31','report_mode':'compare','region':['ap-south-1']},today=date(2026,9,8))
        self.assertEqual((p['compare_start'],p['compare_end']),('2026-05-01','2026-06-30'))
        r=build_report(self.params|{'report_mode':'compare','region':['ap-south-1']})
        self.assertEqual(len(r['query_ids']),2)
        for q in ExplorerQuery.objects.all():self.assertEqual(q.parameters['Filter'],{'Dimensions':{'Key':'REGION','Values':['ap-south-1']}})

    def test_forecast_separates_actual_today_from_future(self):
        p=self.params|{'end':str(self.today+timedelta(days=5)),'forecast':'1'}
        build_report(p)
        actual=ExplorerQuery.objects.get(operation='get_cost_and_usage');future=ExplorerQuery.objects.get(operation='get_cost_forecast')
        self.assertEqual(actual.parameters['TimePeriod']['End'],str(self.today))
        self.assertEqual(future.parameters['TimePeriod']['Start'],str(self.today))
        self.assertEqual(future.parameters['PredictionIntervalLevel'],80)

    def test_metadata_is_authenticated_queued_and_payer_supported(self):
        self.assertEqual(Client().get('/explorer/metadata/?dimension=region').status_code,302)
        with patch('billing.query_cache.cost_client') as aws:
            data=self.client.get('/explorer/metadata/',{'dimension':'payer','start':str(self.start),'end':str(self.today)}).json()
        aws.assert_not_called();self.assertTrue(data['pending']);self.assertEqual(ExplorerQuery.objects.get().parameters['Dimension'],'PAYER_ACCOUNT')

    def test_console_link_and_saved_report_roundtrip(self):
        url='https://us-east-1.console.aws.amazon.com/costmanagement/home?region=ap-south-1#/cost-explorer?chartStyle=STACK&costAggregate=unBlendedCost&endDate=2026-08-31&excludeForecasting=false&filter=%5B%5D&granularity=Monthly&groupBy=%5B%22Service%22%5D&reportMode=STANDARD&reportName=New%20cost%20report&startDate=2026-03-01'
        p=import_console_url(url);self.assertEqual((p['group_by'],p['forecast'],p['start']),('service','1','2026-03-01'))
        with self.assertRaises(ValueError):import_console_url(url.replace('console.aws.amazon.com','attacker.invalid'))
        with self.assertRaises(ValueError):import_console_url(url.replace('%5B%5D','%5B1%5D'))
        result=self.client.post('/reports/save/',querydict(p));self.assertEqual(result.status_code,302)
        saved=SavedReport.objects.get();self.assertEqual(saved.parameters,p);self.assertEqual(self.client.get(f'/reports/{saved.pk}/').status_code,302)
