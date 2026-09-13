"""Behavioral control matrix: filters must conserve costs across include/exclude views."""
import csv
import io
import json
from datetime import date, timedelta
from dateutil.relativedelta import relativedelta
from decimal import Decimal
from urllib.parse import urlencode
from unittest.mock import Mock, patch
from django.contrib.auth.models import User
from django.http import QueryDict
from django.test import TestCase, override_settings
from django.utils import timezone
from billing import parameters as contract
from billing.advanced_explorer import build_report
from billing.models import AccountAssignment, ExplorerQuery
from billing.query_cache import run_query
from .helpers import make_customer, web_settings, cost


@web_settings
class ExplorerControlTests(TestCase):
    def setUp(self):
        self.day=timezone.now().date()-timedelta(days=2)
        self.customer,self.source=make_customer('Connected','111111111111',accounts=('014814866835',))
        self.client.force_login(User.objects.create_user('controls',is_staff=True))
        self.params={'start':str(self.day),'end':str(self.day),'date_range':'custom','group_by':'region','forecast':'0'}

    def result(self, request):
        """Independent mini ledger with unequal costs and a credit; evaluate CE expressions."""
        def matches(expr, row):
            if not expr:return True
            if 'And' in expr:return all(matches(e,row) for e in expr['And'])
            if 'Or' in expr:return any(matches(e,row) for e in expr['Or'])
            if 'Not' in expr:return not matches(expr['Not'],row)
            kind=next(iter(expr));term=expr[kind]
            key=term.get('Key','')
            value=row.get(key,'')
            return not value if 'ABSENT' in term.get('MatchOptions',[]) else value in term.get('Values',[])
        rows=[]
        for account,amount,suffix in [('014814866835','7','a'),('111111111111','19','b'),('111111111111','-2','b')]:
            row={dim:suffix for _,dim in contract.DIMENSIONS.values()}
            row.update(LINKED_ACCOUNT=account,SERVICE=contract.EC2,Team=suffix)
            rows.append((row,Decimal(amount)))
        groups={};metric=request['Metrics'][0];group=request.get('GroupBy',[])
        for row,amount in rows:
            if not matches(request.get('Filter'),row):continue
            key=row[group[0]['Key']] if group else 'Total'
            if group and group[0]['Type'] in ('TAG','COST_CATEGORY'):key=group[0]['Key']+'$'+key
            groups[key]=groups.get(key,Decimal(0))+amount
        metrics=lambda value:{metric:{'Amount':str(value),'Unit':'USD' if metric.endswith('Cost') else 'Hrs'}}
        period={'TimePeriod':request['TimePeriod'],'Estimated':False,'Groups':[],'Total':{}}
        if group:period['Groups']=[{'Keys':[key],'Metrics':metrics(value)} for key,value in groups.items()]
        else:period['Total']=metrics(sum(groups.values(),Decimal(0)))
        return {'ResultsByTime':[period]}

    def collect(self, params):
        build_report(params)
        for q in ExplorerQuery.objects.filter(requested=True):
            client=Mock();getattr(client,q.operation).return_value=self.result(q.parameters)
            run_query(q,client)
        return build_report(params)

    def test_every_filter_include_exclude_conserves_total_and_comparison(self):
        for key in contract.FILTERS:
            for mode,expected in [('include',Decimal(7)),('exclude',Decimal(17))]:
                with self.subTest(filter=key,mode=mode):
                    value='014814866835' if key=='account' else contract.EC2 if key=='service' else 'a'
                    params=self.params|{key:[value],key+'_mode':mode,'report_mode':'compare'}
                    if key=='service':expected=Decimal(24 if mode=='include' else 0)
                    if key in ('tag','cost_category'):params[key+'_key']='Team'
                    if key=='resource':params['service']=[contract.EC2]
                    result=self.collect(params)
                    self.assertEqual(result['total'],expected)
                    self.assertEqual(result['comparison_total'],expected)
                    self.assertTrue(result['comparison_ready'])
                    self.assertTrue(all(r['delta']==0 for r in result['comparison_rows']))

    def test_every_group_and_metric_chart_and_csv_totals_agree(self):
        for group in contract.GROUPS:
            for metric in contract.METRICS:
                with self.subTest(group=group,metric=metric):
                    params=self.params|{'group_by':group,'metric':metric,'report_mode':'compare'}
                    if group in ('tag','cost_category'):params['group_key']='Team'
                    if group=='resource':params['service']=[contract.EC2]
                    result=self.collect(params)
                    self.assertEqual(result['total'],Decimal(24))
                    self.assertEqual(sum(result['period_totals']),Decimal(24))
                    self.assertEqual(sum(result['chart_payload']['totals']),24)
                    response=self.client.get('/export/report/',contract.querydict(params))
                    self.assertEqual(response.status_code,200)
                    rows=list(csv.reader(io.StringIO(response.content.decode())))
                    self.assertEqual(Decimal(rows[1][1]),Decimal(24))

    def test_user_exclusion_url_has_same_filter_in_both_periods(self):
        params=QueryDict('chart_style=stacked&currency=USD&report_mode=compare&date_range=last_6_months&start=2026-03-01&end=2026-08-31&compare_start=&compare_end=&granularity=monthly&group_by=service&account_mode=exclude&account=014814866835&region_mode=exclude&measure=cost&metric=unblended&forecast=0&forecast=1')
        result=self.collect(params)
        self.assertEqual(result['total'],Decimal(17))
        self.assertEqual(result['comparison_total'],Decimal(17))
        self.assertEqual({str(q.parameters['Filter']) for q in ExplorerQuery.objects.all()}, {str({'Not':{'Dimensions':{'Key':'LINKED_ACCOUNT','Values':['014814866835']}}})})

    def test_multi_account_exclusion_inside_customer_keeps_ownership(self):
        params=self.params|{'customer':str(self.customer.pk),'account':['014814866835'],'account_mode':'exclude','report_mode':'compare'}
        self.assertEqual(self.collect(params)['total'],Decimal(17))
        filters=[q.parameters['Filter'] for q in ExplorerQuery.objects.all()]
        self.assertEqual(filters[0],filters[1])
        self.assertIn({'Dimensions':{'Key':'LINKED_ACCOUNT','Values':['014814866835','111111111111']}},filters[0]['And'])
        result=self.collect(params|{'account':['014814866835','111111111111']})
        self.assertEqual(result['total'],0)
        self.assertEqual(result['group_count'],0)  # no invented "Total" service
        self.assertTrue(result['known_empty']);self.assertEqual(result['period_totals'],[0])

    def test_chart_switches_reuse_queries_and_keep_all_parameters(self):
        params=self.params|{'region':['a'],'account_mode':'exclude','account':['014814866835'],'report_mode':'compare'}
        ids=None
        for style in ('line','bar','stacked'):
            result=self.collect(params|{'chart_style':style})
            if ids is not None:self.assertEqual(result['query_ids'],ids)
            ids=result['query_ids'];self.assertEqual(result['chart_payload']['style'],style)
            for option in result['style_options']:
                query=QueryDict(option['url'].split('?',1)[1]);self.assertEqual(query.getlist('account'),['014814866835']);self.assertEqual(query['account_mode'],'exclude')

    def test_pending_customer_does_not_block_connected_export_but_failed_query_does(self):
        make_customer('Pending','222222222222',connected=False)
        result=self.collect(self.params)
        self.assertFalse(result['report_incomplete']);self.assertIn('connected customers only',' '.join(result['warnings']))
        self.assertEqual(self.client.get('/export/report/',self.params).status_code,200)
        make_customer('Failed','333333333333')
        self.assertTrue(build_report(self.params)['report_incomplete'])
        self.assertEqual(self.client.get('/export/report/',self.params).status_code,409)

    def test_metadata_respects_other_filters_exclusion_and_source(self):
        params=self.params|{'dimension':'service','service':['irrelevant'],'region':['a'],'account':['014814866835'],'account_mode':'exclude','source':str(self.source.pk)}
        response=self.client.get('/explorer/metadata/',params)
        self.assertEqual(response.status_code,200)
        q=ExplorerQuery.objects.get(operation='get_dimension_values')
        self.assertEqual(q.parameters['Filter'],{'And':[{'Not':{'Dimensions':{'Key':'LINKED_ACCOUNT','Values':['014814866835']}}},{'Dimensions':{'Key':'REGION','Values':['a']}}]})

    def test_optional_metadata_and_reports_explain_prerequisites_without_500(self):
        with override_settings(REQUIRE_CONNECTION_APPROVAL=True):
            for key in ('tag','cost_category','resource'):
                with self.subTest(dimension=key):
                    response=self.client.get('/explorer/metadata/',self.params|{'dimension':key})
                    self.assertEqual(response.status_code,200)
                    self.assertTrue(response.json()['errors']);self.assertFalse(response.json()['pending'])
            result=build_report(self.params|{'group_by':'tag','group_key':'Team'})
            self.assertTrue(result['report_incomplete']);self.assertFalse(result['report_pending'])
            self.assertTrue(result['warnings']);self.assertFalse(result['has_data'])
            response=self.client.get('/',self.params|{'group_by':'tag','group_key':'Team'})
            self.assertEqual(response.status_code,200)
            self.assertContains(response,'Optional feature availability')

    def test_one_comparison_date_rejected_and_usage_group_supported(self):
        with self.assertRaisesMessage(ValueError,'both comparison dates'):
            contract.normalize(self.params|{'report_mode':'compare','compare_start':str(self.day)})
        result=self.collect(self.params|{'measure':'usage','usage_type_group':['a']})
        self.assertEqual(result['total'],7);self.assertEqual(result['currency'],'Hrs')
        result=self.collect(self.params|{'measure':'usage','usage_type':['a'],'normalized':'1'})
        self.assertEqual(result['total'],7)

    def test_daily_hourly_empty_modes_and_local_drilldown(self):
        cost(self.source,self.day,'24',service=contract.EC2)
        params=self.params|{'group_by':'customer','account_mode':'exclude','service_mode':'exclude'}
        result=build_report(params)
        self.assertNotIn('query_ids',result)
        self.assertIn('customer='+str(self.customer.pk),result['pivot_rows'][0]['url'])
        for granularity in ('daily','hourly'):
            result=self.collect(self.params|{'granularity':granularity})
            self.assertEqual(result['total'],24)
            self.assertEqual(len(result['periods']),24 if granularity=='hourly' else 1)

    def test_absent_tag_drilldown_keeps_key_and_filters(self):
        params=self.params|{'group_by':'tag','group_key':'Team','region':['a']}
        build_report(params);q=ExplorerQuery.objects.get()
        response=self.result(q.parameters);response['ResultsByTime'][0]['Groups'][0]['Keys']=['Team$']
        q.data=response;q.requested=False;q.last_attempt=timezone.now();q.save()
        query=QueryDict(build_report(params)['pivot_rows'][0]['url'].split('?',1)[1])
        self.assertEqual(query['untagged'],'1');self.assertEqual(query['tag_key'],'Team');self.assertEqual(query.getlist('region'),['a'])

    def test_import_observed_aws_exclusion_and_comparison_url(self):
        filters=[{'dimension':{'id':'LinkedAccount','displayValue':'Linked account'},'operator':'EXCLUDES','values':[{'value':'014814866835','displayValue':'Network account'}]}]
        base={'reportMode':'COMPARE','comparisonStartDate':'2026-08-01','comparisonEndDate':'2026-08-31','baselineStartDate':'2026-07-01','baselineEndDate':'2026-07-31','groupBy':'["Service"]','filter':json.dumps(filters)}
        prefix='https://us-east-1.console.aws.amazon.com/costmanagement/home#/cost-explorer?'
        p=contract.import_console_url(prefix+urlencode(base))
        self.assertEqual((p['start'],p['end'],p['compare_start'],p['compare_end']),('2026-08-01','2026-08-31','2026-07-01','2026-07-31'))
        self.assertEqual(p['account'],['014814866835']);self.assertEqual(p['account_mode'],'exclude')
        for bad in [filters*2,[{'dimension':{'id':'Unknown'},'operator':'INCLUDES','values':[]}],[{'dimension':{'id':'Tag'},'operator':'INCLUDES','values':[{'value':'prod'}]}]]:
            with self.assertRaises(ValueError):contract.import_console_url(prefix+urlencode(base|{'filter':json.dumps(bad)}))

    def test_comparison_chart_keeps_periods_separate_and_totals_not_added(self):
        result=self.collect(self.params|{'report_mode':'compare','account_mode':'exclude','account':['014814866835']})
        chart=result['comparison_chart']
        self.assertEqual(chart['style'],'bar');self.assertTrue(chart['comparison'])
        self.assertEqual([sum(s['values']) for s in chart['series']],[17,17])
        self.assertEqual(sum(chart['totals']),0)
        response=self.client.get('/',self.params|{'report_mode':'compare','account_mode':'exclude','account':['014814866835']})
        self.assertContains(response,'id="comparison-chart"');self.assertContains(response,'Previous period')

    def test_historical_ownership_survives_transfer_in_comparison_and_metadata(self):
        month=self.day.replace(day=1);current=month-relativedelta(months=1);prior=month-relativedelta(months=2)
        AccountAssignment.objects.filter(customer=self.customer).update(end=current)
        params=self.params|{'customer':str(self.customer.pk),'report_mode':'compare','start':str(current),'end':str(month-timedelta(days=1)),'compare_start':str(prior),'compare_end':str(current-timedelta(days=1))}
        result=self.collect(params)
        self.assertTrue(result['comparison_ready']);self.assertFalse(result['report_incomplete'])
        self.assertEqual(result['total'],0);self.assertEqual(result['comparison_total'],24)
        self.assertEqual(result['comparison_delta'],-24)
        self.assertEqual(ExplorerQuery.objects.get().parameters['TimePeriod'],{'Start':str(prior),'End':str(current)})
        response=self.client.get('/explorer/metadata/',params|{'start':str(prior),'dimension':'account'})
        self.assertEqual(response.status_code,200)
        q=ExplorerQuery.objects.get(operation='get_dimension_values')
        self.assertEqual(q.parameters['TimePeriod'],{'Start':str(prior),'End':str(current)})
        self.assertEqual(q.parameters['Filter']['Dimensions']['Values'],['014814866835','111111111111'])

    def test_relative_comparisons_roll_on_reopen_and_custom_dates_stay_fixed(self):
        p=contract.normalize({'report_mode':'compare','compare_range':'month_over_month'},today=date(2026,9,10))
        self.assertEqual((p['start'],p['compare_start']),('2026-08-01','2026-07-01'))
        reopened=contract.normalize(p,today=date(2026,10,10))
        self.assertEqual((reopened['start'],reopened['compare_start']),('2026-09-01','2026-08-01'))
        fixed=contract.normalize(p|{'compare_range':'custom','date_range':'custom'},today=date(2026,10,10))
        self.assertEqual((fixed['start'],fixed['compare_start']),('2026-08-01','2026-07-01'))
        previous=contract.normalize({'report_mode':'compare','date_range':'last_month'},today=date(2026,9,10))
        self.assertEqual(contract.normalize(previous,today=date(2026,10,10))['compare_start'],'2026-08-01')

    def test_native_comparison_drivers_paginate_preserve_filters_and_export(self):
        self.source.capabilities={'comparison_drivers':True};self.source.save()
        params={'report_mode':'compare','compare_range':'month_over_month','customer':str(self.customer.pk),'group_by':'service','account':['014814866835'],'account_mode':'exclude'}
        build_report(params)
        driver={'CostSelector':{'Dimensions':{'Key':'SERVICE','Values':['Amazon EC2']}},'CostDrivers':[{'Name':'Usage change','Type':'USAGE','Metrics':{'Cost':{'BaselineTimePeriodAmount':'7','ComparisonTimePeriodAmount':'9','Difference':'2','Unit':'USD'}}}]}
        for q in ExplorerQuery.objects.filter(requested=True):
            client=Mock()
            if q.operation=='get_cost_comparison_drivers':
                self.assertEqual(q.parameters['MetricForComparison'],'UnblendedCost')
                self.assertEqual(q.parameters['Filter']['And'][0],{'Not':{'Dimensions':{'Key':'LINKED_ACCOUNT','Values':['014814866835']}}})
                self.assertEqual(q.parameters['Filter']['And'][1]['Dimensions']['Values'],['014814866835','111111111111'])
                client.get_cost_comparison_drivers.side_effect=[{'CostComparisonDrivers':[driver],'NextPageToken':'next'},{'CostComparisonDrivers':[driver]}]
            else:getattr(client,q.operation).return_value=self.result(q.parameters)
            run_query(q,client)
        result=build_report(params)
        self.assertEqual(len(result['driver_rows']),2);self.assertEqual(result['driver_rows'][0]['facts'][0]['delta'],2)
        self.assertFalse(result['report_incomplete'])
        drill=QueryDict(result['comparison_rows'][0]['url'].split('?',1)[1])
        self.assertEqual(drill['group_by'],'usage_type');self.assertEqual(drill.getlist('account'),['014814866835']);self.assertEqual(drill['account_mode'],'exclude')
        export=self.client.get('/export/report/',contract.querydict(params))
        self.assertEqual(export.status_code,200);self.assertIn('Usage change',export.content.decode())

    def test_optional_drivers_never_block_core_or_widen_temporal_scope(self):
        from billing.comparison_drivers import build
        params=contract.normalize({'report_mode':'compare','compare_range':'month_over_month','customer':str(self.customer.pk),'group_by':'region'})
        units=[(self.source,self.customer,['014814866835','111111111111'])]
        self.assertIn('ce:GetCostComparisonDrivers',build(params,units)['notes'][0])
        self.assertFalse(ExplorerQuery.objects.exists())
        self.source.capabilities={'comparison_drivers':True};self.source.save()
        AccountAssignment.objects.filter(customer=self.customer).update(end=date.fromisoformat(params['start'])+timedelta(days=5))
        self.assertIn('ownership changed',build(params,units)['notes'][0]);self.assertFalse(ExplorerQuery.objects.exists())
        AccountAssignment.objects.filter(customer=self.customer).update(end=None)
        malformed=Mock(data={'CostComparisonDrivers':[{'CostDrivers':[{'Metrics':{'Cost':{'BaselineTimePeriodAmount':'NaN'}}}]}]},error='',requested=False)
        with patch('billing.comparison_drivers.get_query',return_value=malformed):
            result=build(params,units)
        self.assertEqual(result['rows'],[]);self.assertIn('could not be interpreted',result['notes'][0])

    def test_forecast_does_not_include_accounts_owned_only_in_the_past(self):
        today=timezone.now().date()
        AccountAssignment.objects.filter(customer=self.customer,account__account_id='014814866835').update(end=today)
        params=self.params|{'customer':str(self.customer.pk),'end':str(today+timedelta(days=5)),'forecast':'1'}
        build_report(params)
        future=ExplorerQuery.objects.get(operation='get_cost_forecast')
        self.assertEqual(future.parameters['Filter'],{'Dimensions':{'Key':'LINKED_ACCOUNT','Values':['111111111111']}})
        client=Mock();client.get_cost_forecast.return_value={'ForecastResultsByTime':[{'TimePeriod':future.parameters['TimePeriod'],'MeanValue':'10.5','PredictionIntervalLowerBound':'8','PredictionIntervalUpperBound':'12'}]}
        run_query(future,client)
        result=build_report(params)
        self.assertEqual(result['forecast_rows'][0]['mean'],Decimal('10.5'))
        response=self.client.get('/',params);self.assertContains(response,'AWS cost forecast');self.assertContains(response,'$10.50')
