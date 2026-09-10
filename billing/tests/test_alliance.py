import csv
import io
from datetime import date, datetime, timezone as dt_timezone
from decimal import Decimal
from unittest.mock import patch
from dateutil.relativedelta import relativedelta
from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse
from billing import alliance
from billing.models import AllianceRecord, AllianceServiceNote, AuditEvent, AwsAccount, CollectionPeriod, Cost
from .helpers import assign, cost, make_customer, web_settings


@web_settings
class AllianceTests(TestCase):
    def setUp(self):
        clock = patch('django.utils.timezone.now',return_value=datetime(2026,9,9,12,tzinfo=dt_timezone.utc))
        clock.start(); self.addCleanup(clock.stop)
        self.user = User.objects.create_user('operator',is_staff=True)
        self.client.force_login(self.user)
        self.customer,self.source = make_customer('Customer One','111111111111')
        self.account = AwsAccount.objects.get(account_id=self.source.account_id)
        self.month,self.prior = date(2026,8,1),date(2026,7,1)
        cost(self.source,self.prior,100)
        cost(self.source,self.month,115)
        for month in (self.month,self.prior):
            self.cover(month)
        self.url = reverse('alliance_detail',args=[self.customer.pk,self.account.account_id])+'?month=2026-08'

    def cover(self,month,**changes):
        return CollectionPeriod.objects.create(source=self.source,month=month,status='complete',first_day=month,
            last_day=month+relativedelta(months=1)-relativedelta(days=1),estimated=changes.get('estimated',False),last_success=datetime(2026,9,8,tzinfo=dt_timezone.utc))

    def post(self,**changes):
        data = {'version':0,'action':'save','account_name':'Main account','legal_entity':'Customer One Pvt Ltd','ace_opportunity_id':'O-123',
                'prepared_by':'Analyst','notes':'','summary_note':''}
        data.update(changes)
        return self.client.post(self.url,data)

    def row(self,**kwargs):
        return alliance.summary(kwargs.get('month',self.month),kwargs.get('currency','USD'),Decimal('15'),kwargs.get('customer',self.customer))['rows'][0]

    def test_workbook_summary_calculations_and_columns(self):
        response = self.client.get('/alliance/?month=2026-08&tab=summary')
        self.assertEqual(response.status_code,200)
        self.assertContains(response,'Customer Legal Entity')
        self.assertContains(response,'ACE Opportunity ID')
        row = self.row()
        self.assertEqual((row['fy_total'],row['average'],row['delta'],row['percent'],row['flag']),(Decimal(215),Decimal('107.5'),Decimal(15),Decimal(15),'REVIEW'))
        self.assertEqual(row['months_loaded'],2)
        self.assertEqual(row['cells'][0]['state'],'No data')
        self.assertIsNone(row['cells'][-1]['value'])
        self.assertEqual(row['cells'][-1]['state'],'Future')
        self.assertEqual(self.client.get(self.url).status_code,200)

    def test_executive_default_and_complete_month_comparison(self):
        response = self.client.get('/alliance/?month=2026-08')
        self.assertContains(response, 'Executive overview')
        self.assertContains(response, 'Customer billing comparison')
        self.assertContains(response, 'Largest account changes')
        self.assertNotContains(response, 'Scroll across for all 12 months')
        executive = response.context['executive']
        self.assertEqual(executive['comparison']['delta'], Decimal('15'))
        self.assertEqual(executive['comparison']['percent'], Decimal('15'))
        self.assertEqual(executive['review_count'], 1)
        self.assertEqual(executive['groups'][0]['count'], 1)
        self.assertEqual(executive['movements'][0]['account_id'], self.account.account_id)

    def test_executive_partial_month_does_not_claim_savings(self):
        CollectionPeriod.objects.filter(source=self.source, month=self.month).update(status='failed')
        response = self.client.get('/alliance/?month=2026-08')
        executive = response.context['executive']
        self.assertEqual(response.context['spend'], Decimal('115'))
        self.assertEqual(executive['data_count'], 1)
        self.assertIsNone(executive['comparison']['delta'])
        self.assertIsNone(executive['groups'][0]['comparison']['percent'])
        self.assertEqual(executive['movements'], [])
        self.assertContains(response, 'Complete months needed')
        response = self.client.get('/alliance/?month=2026-08&export=executive_csv')
        exported = list(csv.DictReader(io.StringIO(response.content.decode())))
        self.assertEqual(exported[0]['Change'], '')
        self.assertEqual(exported[0]['Review'], 'Check data')

    def test_executive_export_and_zero_baseline(self):
        Cost.objects.filter(source=self.source, day=self.prior).update(unblended=0)
        response = self.client.get('/alliance/?month=2026-08')
        self.assertContains(response, 'New from zero')
        self.assertIsNone(response.context['executive']['comparison']['percent'])
        response = self.client.get('/alliance/?month=2026-08&export=executive_customers_csv')
        exported = list(csv.DictReader(io.StringIO(response.content.decode())))
        self.assertEqual(Decimal(exported[0]['Change']), Decimal('115'))
        self.assertEqual(exported[0]['Change percent'], '')
        self.assertEqual(len(exported), 1)

    def test_decrease_and_zero_baseline(self):
        self.assertEqual(alliance.variance(Decimal(85),Decimal(100))['flag'],'REVIEW')
        self.assertEqual(alliance.variance(Decimal(1),Decimal(0))['flag'],'REVIEW')
        self.assertIsNone(alliance.variance(Decimal(1),Decimal(0))['percent'])
        self.assertEqual(alliance.variance(Decimal(0),Decimal(0))['percent'],0)
        self.assertEqual(alliance.variance(None,Decimal(1))['flag'],'No comparison')

    def test_april_uses_march_outside_financial_year(self):
        cost(self.source,date(2026,3,1),80)
        cost(self.source,date(2026,4,1),100)
        self.cover(date(2026,3,1)); self.cover(date(2026,4,1))
        row = self.row(month=date(2026,4,1))
        self.assertEqual(row['prior']['value'],80)
        self.assertEqual(row['percent'],25)
        self.assertEqual(row['fy_total'],315)
        data = alliance.summary(date(2027,1,1),'USD',Decimal(15),self.customer)
        self.assertEqual(data['fy_start'],date(2026,4,1))

    def test_verified_zero_requires_coverage_and_currency_evidence(self):
        other = assign('222222222222',self.customer,self.source).account
        data = alliance.SpendData(self.prior,date(2026,9,1),'USD',self.customer)
        self.assertEqual(data.cell((self.customer.pk,other.account_id),self.month)['value'],0)
        data = alliance.SpendData(self.prior,date(2026,9,1),'EUR',self.customer)
        self.assertIsNone(data.cell((self.customer.pk,other.account_id),self.month)['value'])
        CollectionPeriod.objects.filter(month=self.month).update(status='failed')
        data = alliance.SpendData(self.prior,date(2026,9,1),'USD',self.customer)
        self.assertIsNone(data.cell((self.customer.pk,other.account_id),self.month)['value'])
        self.assertEqual(data.cell((self.customer.pk,self.account.account_id),self.month)['state'],'Partial')

    def test_currency_and_customer_ownership_are_isolated(self):
        cost(self.source,self.month,900,currency='EUR')
        second,_ = make_customer('Customer Two','333333333333')
        assign(self.account.account_id,second,self.source,start=self.month)
        Cost.objects.filter(source=self.source,day__gte=self.month).update(customer=second)
        self.assertIsNone(self.row()['current']['value'])
        rows = alliance.summary(self.month,'USD',Decimal(15),second)['rows']
        row = next(r for r in rows if r['account_id']==self.account.account_id)
        self.assertEqual(row['current']['value'],115)
        self.assertIsNone(row['prior']['value'])
        self.assertEqual(alliance.summary(self.month,'EUR',Decimal(15),second)['rows'][0]['current']['value'],900)
        unrelated,_ = make_customer('Unrelated','444444444444')
        url = reverse('alliance_detail',args=[unrelated.pk,self.account.account_id])+'?month=2026-08'
        self.assertEqual(self.client.get(url).status_code,400)
        self.assertEqual(self.client.post(url,{'version':0}).status_code,400)

    def test_record_snapshot_save_comment_and_export(self):
        response = self.client.get(self.url)
        field = response.context['form'].service_fields[0][1]
        response = self.post(action='capture',**{field:'EC2 capacity increased'},summary_note='Reviewed with operations')
        self.assertEqual(response.status_code,302)
        record = AllianceRecord.objects.get()
        self.assertEqual(Decimal(record.snapshot['current']),115)
        self.assertEqual(record.bill_pulled_on,date(2026,9,9))
        self.assertEqual(AllianceServiceNote.objects.get().commentary,'EC2 capacity increased')
        before = record.snapshot.copy()
        self.assertEqual(self.post(version=1,notes='Internal note',**{field:'Confirmed change'}).status_code,302)
        record.refresh_from_db()
        self.assertEqual(record.snapshot,before)
        self.assertEqual(record.status,'Ready to share')
        self.assertEqual(AuditEvent.objects.filter(action__startswith='Alliance').count(),2)
        for url in (self.url,self.url+'&export=csv','/alliance/?month=2026-08&tab=handoff','/alliance/?month=2026-08&tab=handoff&export=csv'):
            self.assertEqual(self.client.get(url).status_code,200)

    def test_metadata_carries_forward_but_handoff_does_not(self):
        self.post(action='capture')
        september = self.client.get(self.url.replace('2026-08','2026-09'))
        self.assertEqual(september.context['form']['legal_entity'].value(),'Customer One Pvt Ltd')
        self.assertFalse(september.context['record'].snapshot)
        self.assertFalse(september.context['record'].apn_marked)
        self.assertIsNone(september.context['record'].bill_pulled_on)
        july = self.client.get(self.url.replace('2026-08','2026-07'))
        self.assertEqual(july.context['record'].legal_entity,'')

    def test_stale_writer_cannot_overwrite_or_duplicate(self):
        self.assertEqual(self.post().status_code,302)
        self.assertEqual(self.post(legal_entity='Stale edit').status_code,409)
        self.assertEqual(AllianceRecord.objects.count(),1)
        self.assertEqual(AllianceRecord.objects.get().legal_entity,'Customer One Pvt Ltd')

    def closure(self,**changes):
        data = {'version':1,'bill_pulled_on':'2026-09-09','shared_with':'Alliance owner','date_shared':'2026-09-09','apn_marked':'on','apn_marked_date':'2026-09-09'}
        data.update(changes)
        return self.post(**data)

    def test_close_then_aws_revision_reopens_review_without_overwriting(self):
        self.post(action='capture')
        self.assertEqual(self.closure().status_code,302)
        self.assertEqual(self.row()['status'],'Closed')
        self.assertEqual(self.closure(version=2,action='capture').status_code,200)
        # A service mix revision with unchanged total still needs review.
        Cost.objects.filter(source=self.source,day=self.month).update(service='Amazon S3')
        row = self.row()
        self.assertTrue(row['revised'])
        self.assertEqual(row['status'],'Cost revision needs review')
        response = self.closure(version=2)
        self.assertContains(response,'closure requires')
        record = AllianceRecord.objects.get()
        self.assertEqual(record.revision,2)
        self.assertEqual(record.snapshot['services'][0]['service'],'Amazon EC2')
        self.assertEqual(self.post(version=2,apn_marked='',apn_marked_date='').status_code,302)
        self.assertEqual(self.post(version=3,action='capture').status_code,302)
        self.assertFalse(self.row()['revised'])

    def test_threshold_change_is_not_an_aws_revision(self):
        self.post(action='capture')
        row = alliance.summary(self.month,'USD',Decimal('20'),self.customer)['rows'][0]
        self.assertFalse(row['revised'])
        self.assertEqual(row['flag'],'OK')
        self.assertEqual(row['recorded_percent'],15)

    def test_prior_month_correction_is_detected(self):
        self.post(action='capture')
        Cost.objects.filter(day=self.prior).update(unblended=90)
        self.assertTrue(self.row()['revised'])

    def test_incomplete_or_estimated_spend_cannot_close(self):
        CollectionPeriod.objects.filter(month=self.month).update(estimated=True)
        self.post(action='capture')
        self.assertContains(self.closure(),'closure requires')
        self.assertFalse(AllianceRecord.objects.get().apn_marked)

    def test_dates_and_required_closure_fields(self):
        self.assertContains(self.post(date_shared='2026-09-09'),'Record the AWS figures')
        self.assertEqual(AllianceRecord.objects.count(),0)
        self.post(action='capture')
        for changes in ({'apn_marked_date':'2026-09-10'}, {'date_shared':'2026-09-08'}, {'legal_entity':''}, {'ace_opportunity_id':''}, {'blocked':'on','notes':'Awaiting approval'}):
            self.assertEqual(self.closure(**changes).status_code,200)
            self.assertFalse(AllianceRecord.objects.get().apn_marked)
        self.assertEqual(self.closure().status_code,302)

    def test_readers_and_csrf(self):
        reader = User.objects.create_user('reader')
        self.client.force_login(reader)
        self.assertEqual(self.client.get(self.url).status_code,200)
        self.assertEqual(self.post().status_code,403)
        csrf = Client(enforce_csrf_checks=True); csrf.force_login(self.user)
        self.assertEqual(csrf.post(self.url,{'version':0}).status_code,403)
        anonymous = Client()
        self.assertEqual(anonymous.get('/alliance/').status_code,302)

    def test_exports_preserve_all_workbook_columns_and_escape_formula_text(self):
        self.post(account_name='=HYPERLINK("bad")',action='capture')
        response = self.client.get('/alliance/?month=2026-08&export=csv')
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[0][:5],['S.No','Account','Customer Legal Entity (as in Partner Central)','AWS Account ID','ACE Opportunity ID'])
        self.assertEqual(rows[0][5:17],['Apr-26','May-26','Jun-26','Jul-26','Aug-26','Sep-26','Oct-26','Nov-26','Dec-26','Jan-27','Feb-27','Mar-27'])
        self.assertEqual(rows[0][17:24],['FY Total','Avg / Month','Reporting Mth','Prior Mth','MoM Var','MoM Var %','Flag'])
        self.assertTrue(rows[1][1].startswith("'="))
        handoff = self.client.get('/alliance/?month=2026-08&tab=handoff&export=csv').content.decode()
        self.assertIn('APN Marked (Y/N)',handoff)
        self.assertIn('Notes / Blocker',handoff)
        self.assertContains(self.client.get(self.url+'&export=csv'),'Variance Driver / Internal Commentary')

    def test_invalid_filters(self):
        for query in ('month=oops','month=9999-12','currency=usd','threshold=NaN','threshold=-1','customer=invalid'):
            self.assertEqual(self.client.get('/alliance/?'+query).status_code,400)
