"""Regression tests for controls observed in the 2026-09-22 AWS console audit."""
import csv
import io
import json
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode, urlsplit
from unittest.mock import Mock

from django.contrib.auth.models import User
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from dateutil.relativedelta import relativedelta

from billing import parameters as contract
from billing.advanced_explorer import build_report, is_local
from billing.models import CustomerApproval, ExplorerQuery
from billing.query_cache import run_query
from .helpers import make_customer, web_settings


class ExplorerDateParityTests(SimpleTestCase):
    today = date(2026, 9, 22)

    def test_live_historical_presets(self):
        expected = {
            'last_1_day': ('2026-09-21', '2026-09-21'),
            'last_7_days': ('2026-09-16', '2026-09-22'),
            'last_14_days': ('2026-09-09', '2026-09-22'),
            'this_month': ('2026-09-01', '2026-09-22'),
            'last_month': ('2026-08-01', '2026-08-31'),
            'last_3_months': ('2026-06-01', '2026-08-31'),
            'last_6_months': ('2026-03-01', '2026-08-31'),
            'last_12_months': ('2025-09-01', '2026-08-31'),
            'last_36_months': ('2023-09-01', '2026-08-31'),
            'year_to_date': ('2026-01-01', '2026-09-22'),
            'current_month': ('2026-09-01', '2026-09-30'),
        }
        for selected, dates in expected.items():
            with self.subTest(selected=selected):
                p = contract.normalize({'date_range': selected}, today=self.today)
                self.assertEqual((p['start'], p['end']), dates)
                self.assertEqual(p['historical_end'], dates[1])

    def test_future_presets_combine_and_roll_when_reopened(self):
        for future, end in [('next_1_month', '2026-10-31'), ('next_3_months', '2026-12-31'),
                            ('next_12_months', '2027-09-30'), ('next_18_months', '2028-03-31')]:
            with self.subTest(future=future):
                p = contract.normalize({'date_range': 'last_6_months', 'future_range': future}, today=self.today)
                self.assertEqual((p['start'], p['historical_end'], p['end']), ('2026-03-01', '2026-08-31', end))
                reopened = contract.normalize(contract.querydict(p), today=date(2026, 10, 5))
                self.assertEqual((reopened['start'], reopened['historical_end']), ('2026-04-01', '2026-09-30'))
                self.assertGreater(reopened['end'], p['end'])

    def test_custom_historical_dates_stay_fixed_while_future_rolls(self):
        p = contract.normalize({'start': '2026-07-10', 'end': '2026-08-15', 'future_range': 'next_1_month'}, today=self.today)
        reopened = contract.normalize(contract.querydict(p), today=date(2026, 10, 5))
        self.assertEqual((reopened['start'], reopened['historical_end'], reopened['end']), ('2026-07-10', '2026-08-15', '2026-11-30'))
        absolute = contract.normalize({'start': '2026-07-10', 'end': '2026-11-15'}, today=self.today)
        self.assertEqual(contract.normalize(absolute, today=date(2026, 10, 5))['end'], '2026-11-15')

    def test_granularity_and_forecast_limits_remain_explicit(self):
        with self.assertRaisesMessage(ValueError, 'Monthly granularity'):
            contract.normalize({'date_range': 'last_36_months', 'granularity': 'daily'}, today=self.today)
        with self.assertRaisesMessage(ValueError, 'three months'):
            contract.normalize({'date_range': 'last_month', 'future_range': 'next_12_months', 'granularity': 'daily'}, today=self.today)
        with self.assertRaisesMessage(ValueError, 'forecasted costs'):
            contract.normalize({'date_range': 'last_month', 'future_range': 'next_1_month', 'forecast': '0'}, today=self.today)
        with self.assertRaisesMessage(ValueError, '38 months'):
            contract.normalize({'start': '2020-01-01', 'end': '2020-01-31'}, today=self.today)
        with self.assertRaisesMessage(ValueError, 'historical start'):
            contract.normalize({'start': '2026-09-20', 'end': '2026-08-01', 'future_range': 'next_1_month'}, today=self.today)
        with self.assertRaisesMessage(ValueError, 'valid historical'):
            contract.normalize({'future_range': 'next_100_months'}, today=self.today)

    def test_observed_tag_link_and_relative_ranges_roundtrip(self):
        filters = [{'dimension': {'id': 'TagKey', 'displayValue': 'Tag'}, 'operator': 'EXCLUDES',
                    'values': [{'value': 'DEV', 'displayValue': 'DEV'}],
                    'growableValue': {'value': 'Environment', 'displayValue': 'Environment'}}]
        url = 'https://us-east-1.console.aws.amazon.com/costmanagement/home#/cost-explorer?'+urlencode({
            'historicalRelativeRange': 'LAST_3_YEARS',
            'groupBy': '["TagKeyValue:Environment"]', 'filter': json.dumps(filters), 'reportMode': 'STANDARD'})
        p = contract.import_console_url(url, today=self.today)
        self.assertEqual((p['group_by'], p['group_key'], p['tag_key'], p['tag'], p['tag_mode']),
                         ('tag', 'Environment', 'Environment', ['DEV'], 'exclude'))
        self.assertEqual((p['start'], p['end']), ('2023-09-01', '2026-08-31'))
        result = contract.import_console_url(contract.export_console_url(p, today=self.today), today=self.today)
        self.assertEqual(result, p)

    def test_native_future_enum_and_grouping_boundary(self):
        url = 'https://console.aws.amazon.com/costmanagement/home#/cost-explorer?'+urlencode({
            'historicalRelativeRange': 'LAST_3_MONTHS', 'futureRelativeRange': 'NEXT_MONTH', 'groupBy': '[]'})
        p = contract.import_console_url(url, today=self.today)
        self.assertEqual((p['future_range'], p['end']), ('next_1_month', '2026-10-31'))
        exported = contract.export_console_url(p, today=self.today)
        self.assertIn('futureRelativeRange=NEXT_MONTH', exported)
        for date_range, native in [('last_month', 'LAST_MONTH'), ('last_12_months', 'LAST_12_MONTHS')]:
            self.assertIn('historicalRelativeRange='+native,
                          contract.export_console_url({'date_range': date_range}, today=self.today))
        self.assertEqual(contract.import_console_url(exported, today=self.today), p)
        with self.assertRaisesMessage(ValueError, 'Group by None'):
            contract.export_console_url(p | {'group_by': 'service'}, today=self.today)

    def test_missing_tag_value_and_literal_unassigned_stay_distinct(self):
        p = contract.normalize({'date_range': 'last_month', 'tag_key': 'Team',
                                'tag': [contract.EMPTY_VALUE, '(Unassigned)']}, today=self.today)
        imported = contract.import_console_url(contract.export_console_url(p, today=self.today), today=self.today)
        self.assertEqual(imported['tag'], p['tag'])
        self.assertEqual(contract.expression(imported), contract.expression(p))

    def test_native_dual_measure_import_and_validation(self):
        p = contract.normalize({'date_range': 'last_month', 'measure': 'cost_usage',
                                'usage_type': ['synthetic-hours'], 'metric': 'amortized'}, today=self.today)
        imported = contract.import_console_url(contract.export_console_url(p, today=self.today), today=self.today)
        self.assertEqual((imported['measure'], imported['metric'], imported['usage_type']),
                         ('cost_usage', 'amortized', ['synthetic-hours']))
        with self.assertRaisesMessage(ValueError, 'one usage type'):
            contract.normalize({'measure': 'cost_usage'}, today=self.today)
        with self.assertRaisesMessage(ValueError, 'combined cost and usage'):
            contract.normalize(p | {'future_range': 'next_1_month'}, today=self.today)
        normalized = contract.normalize(p | {'normalized': '1'}, today=self.today)
        self.assertEqual(normalized['normalized'], '1')

    def test_multiple_keyed_filters_combine_and_roundtrip(self):
        additional = [{'type': 'tag', 'key': 'Environment', 'values': ['DEV'], 'mode': 'exclude', 'absent': False},
                      {'type': 'tag', 'key': 'Owner', 'values': ['team'], 'mode': 'include', 'absent': False}]
        p = contract.normalize({'date_range': 'last_month', 'tag_key': 'Team', 'tag': ['ops'],
                                'keyed_filters': json.dumps(additional)}, today=self.today)
        expected = {'And': [{'Tags': {'Key': 'Team', 'Values': ['ops']}},
                            {'Not': {'Tags': {'Key': 'Environment', 'Values': ['DEV']}}},
                            {'Tags': {'Key': 'Owner', 'Values': ['team']}}]}
        self.assertEqual(contract.expression(p), expected)
        self.assertEqual(contract.normalize(contract.querydict(p), today=self.today), p)
        imported = contract.import_console_url(contract.export_console_url(p, today=self.today), today=self.today)
        self.assertEqual(contract.expression(imported), expected)
        self.assertEqual(len(contract.keyed_filters(imported)), 2)

    def test_primary_missing_key_respects_include_and_exclude(self):
        for kind, flag, aws_kind in [('tag','untagged','Tags'), ('cost_category','uncategorized','CostCategories')]:
            for mode in ('include','exclude'):
                with self.subTest(kind=kind,mode=mode):
                    p = contract.normalize({'date_range':'last_month', kind+'_key':'Team', flag:'1', kind+'_mode':mode}, today=self.today)
                    term = {aws_kind:{'Key':'Team','MatchOptions':['ABSENT']}}
                    self.assertEqual(contract.expression(p), {'Not':term} if mode=='exclude' else term)

    def test_additional_categories_and_absence_are_api_filters(self):
        p = contract.normalize({'date_range': 'last_month', 'keyed_filters': [
            {'type': 'cost_category', 'key': 'BusinessUnit', 'values': ['Engineering'], 'mode': 'exclude'},
            {'type': 'tag', 'key': 'Owner', 'values': [], 'absent': True}]}, today=self.today)
        self.assertEqual(contract.expression(p), {'And': [
            {'Not': {'CostCategories': {'Key': 'BusinessUnit', 'Values': ['Engineering']}}},
            {'Tags': {'Key': 'Owner', 'MatchOptions': ['ABSENT']}}]})
        with self.assertRaisesMessage(ValueError, 'not been verified'):
            contract.export_console_url(p, today=self.today)

    def test_additional_keyed_filter_validation_and_drilldown(self):
        basic = {'type': 'tag', 'key': 'Owner', 'values': ['alice']}
        invalid = [[basic, basic], [basic | {'type': 'account'}], [basic | {'values': ['a']*101}],
                   [basic | {'absent': True}], [basic | {'key': ''}], [basic | {'mode': 'ignore'}],
                   [basic | {'account': 'unexpected'}], [basic | {'key': 'k'*121}]]
        for entries in invalid:
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                contract.normalize({'date_range': 'last_month', 'keyed_filters': entries}, today=self.today)
        p = contract.normalize({'date_range': 'last_month', 'tag_key': 'Team', 'tag': ['ops'],
                                'keyed_filters': [basic]}, today=self.today)
        with self.assertRaisesMessage(ValueError, 'including the primary'):
            contract.normalize(p | {'tag_key': 'Owner'}, today=self.today)
        drill = contract.normalize(p | contract.keyed_drilldown(p, 'tag', 'Owner', 'bob'), today=self.today)
        self.assertEqual(contract.expression(drill), {'And': [
            {'Tags': {'Key': 'Owner', 'Values': ['bob']}}, {'Tags': {'Key': 'Team', 'Values': ['ops']}}]})

    def test_compare_console_handoff_retains_periods_and_exclusions(self):
        p = contract.normalize({'report_mode': 'compare', 'compare_range': 'month_over_month',
                                'region': ['synthetic-region'], 'region_mode': 'exclude',
                                'metric': 'net_amortized'}, today=self.today)
        result = contract.import_console_url(contract.export_console_url(p, today=self.today), today=self.today)
        for key in ('start', 'end', 'compare_start', 'compare_end', 'compare_range', 'metric', 'region', 'region_mode'):
            self.assertEqual(result[key], p[key])

    def test_console_export_never_serializes_local_connection_identifiers(self):
        p = {'date_range': 'last_month', 'customer': 'private-customer', 'source': 'private-source'}
        url = contract.export_console_url(p, today=self.today)
        self.assertNotIn('private-', url)
        with self.assertRaisesMessage(ValueError, 'Customer grouping is local'):
            contract.export_console_url(p | {'group_by': 'customer'}, today=self.today)
        bad = 'https://us-east-1.console.aws.amazon.com/costmanagement/home#/cost-explorer?historicalRelativeRange=FUTURE_UNKNOWN'
        with self.assertRaisesMessage(ValueError, 'Unsupported historical'):
            contract.import_console_url(bad, today=self.today)


@web_settings
class ExplorerBackendParityTests(TestCase):
    def setUp(self):
        self.customer, self.source = make_customer('Synthetic parity', '111111111111')
        self.client.force_login(User.objects.create_user('parity', is_staff=True))
        self.today = timezone.now().date()
        self.day = self.today-timedelta(days=2)

    def test_long_monthly_forecast_metadata_queries_only_actual_dates(self):
        response = self.client.get('/explorer/metadata/', {'dimension': 'region', 'date_range': 'last_6_months',
                                                        'future_range': 'next_18_months', 'granularity': 'monthly'})
        self.assertEqual(response.status_code, 200)
        query = ExplorerQuery.objects.get(operation='get_dimension_values')
        self.assertEqual(query.parameters['TimePeriod']['End'], str(self.today+timedelta(days=1)))
        self.assertNotIn('Granularity', query.parameters)

    def test_three_year_report_queues_aws_and_explains_opt_in(self):
        params = {'date_range': 'last_36_months', 'granularity': 'monthly'}
        self.assertFalse(is_local(contract.normalize(params)))
        result = build_report(params)
        self.assertTrue(result['report_incomplete'])
        self.assertTrue(result['report_pending'])
        self.assertIn('multi-year', ' '.join(result['capability_notes']))
        query = ExplorerQuery.objects.get(operation='get_cost_and_usage')
        self.assertEqual(query.parameters['TimePeriod']['Start'], str(self.today.replace(day=1)-relativedelta(months=36)))

    def test_empty_key_and_literal_placeholder_never_merge_or_share_drilldown(self):
        for grouping in ('tag', 'cost_category'):
            with self.subTest(grouping=grouping):
                params = {'start': str(self.day), 'end': str(self.day), 'group_by': grouping, 'group_key': 'Team'}
                result = build_report(params)
                query = ExplorerQuery.objects.get(pk=result['query_ids'][0])
                groups = [{'Keys': [('Team$' if grouping == 'tag' else '')+value], 'Metrics': {'UnblendedCost': {'Amount': amount, 'Unit': 'USD'}}}
                          for value, amount in [('', '2'), ('(Unassigned)', '3'), ('ops$platform', '5')]]
                client = Mock()
                client.get_cost_and_usage.return_value = {'ResultsByTime': [{
                    'TimePeriod': query.parameters['TimePeriod'], 'Groups': groups}]}
                run_query(query, client)
                rows = {row['key']: row for row in build_report(params)['pivot_rows']}
                self.assertEqual(set(rows), {'', '(Unassigned)', 'ops$platform'})
                self.assertEqual(rows['']['total'], Decimal('2'))
                self.assertNotEqual(rows['']['label'], rows['(Unassigned)']['label'])
                self.assertEqual(rows['(Unassigned)']['total'], Decimal('3'))
                absent = QueryDict(urlsplit(rows['']['url']).query)
                literal = QueryDict(urlsplit(rows['(Unassigned)']['url']).query)
                flag = 'untagged' if grouping == 'tag' else 'uncategorized'
                self.assertEqual(absent[flag], '1')
                self.assertEqual(literal[flag], '0')
                self.assertEqual(literal.getlist(grouping), ['(Unassigned)'])

    def test_combined_cost_usage_keeps_units_queries_and_pending_state_separate(self):
        params = {'start': str(self.day), 'end': str(self.day), 'customer': str(self.customer.pk),
                  'group_by': 'region', 'measure': 'cost_usage', 'usage_type': ['synthetic-hours']}
        initial = build_report(params)
        self.assertEqual(initial['params']['measure'], 'cost_usage')
        self.assertEqual(len(initial['query_ids']), 2)
        queries = {q.parameters['Metrics'][0]: q for q in ExplorerQuery.objects.all()}
        self.assertEqual(set(queries), {'UnblendedCost', 'UsageQuantity'})
        self.assertEqual(queries['UnblendedCost'].parameters['Filter'], queries['UsageQuantity'].parameters['Filter'])
        self.assertEqual(queries['UnblendedCost'].parameters['TimePeriod'], queries['UsageQuantity'].parameters['TimePeriod'])
        for metric, amount, unit in [('UnblendedCost', '12', 'USD'), ('UsageQuantity', '5', 'Hrs')]:
            query = queries[metric]
            client = Mock()
            client.get_cost_and_usage.return_value = {'ResultsByTime': [{'TimePeriod': query.parameters['TimePeriod'],
                'Groups': [{'Keys': ['synthetic-region'], 'Metrics': {metric: {'Amount': amount, 'Unit': unit}}}]}]}
            run_query(query, client)
            if metric == 'UnblendedCost':
                pending = build_report(params)
                self.assertTrue(pending['has_data'])
                self.assertTrue(pending['report_incomplete'])
                self.assertTrue(pending['report_pending'])
                self.assertEqual(self.client.get('/export/report/', params).status_code, 409)
        result = build_report(params)
        self.assertEqual((result['total'], result['currency'], result['measure']), (Decimal('12'), 'USD', 'cost'))
        self.assertEqual((result['usage_report']['total'], result['usage_report']['currency'], result['usage_report']['measure']),
                         (Decimal('5'), 'Hrs', 'usage'))
        self.assertFalse(result['report_pending'])
        self.assertFalse(result['report_incomplete'])
        self.assertEqual(result['chart_payload']['totals'], [12.0])
        self.assertEqual(result['usage_report']['chart_payload']['totals'], [5.0])
        self.assertEqual(QueryDict(result['export_query'])['measure'], 'cost_usage')
        self.assertEqual(QueryDict(urlsplit(result['pivot_rows'][0]['url']).query)['measure'], 'cost_usage')
        response = self.client.get('/', params)
        self.assertContains(response, 'Total cost')
        self.assertContains(response, 'Total usage')
        self.assertContains(response, 'Cost (USD)')
        self.assertContains(response, 'Usage (Hrs)')
        self.assertContains(response, '<option value="cost_usage" selected>Cost and usage</option>', html=True)
        exported = self.client.get('/export/report/', params)
        self.assertEqual(exported.status_code, 200)
        csv_rows = list(csv.reader(io.StringIO(exported.content.decode())))
        self.assertIn(['Total costs', '12', '12'], csv_rows)
        self.assertIn(['Usage quantity', 'Unit', 'Hrs'], csv_rows)
        self.assertIn(['Total usage', '5', '5'], csv_rows)

    def test_combined_comparison_csv_requires_both_measures_and_periods(self):
        params = {'report_mode': 'compare', 'compare_range': 'month_over_month',
                  'group_by': 'region', 'measure': 'cost_usage', 'usage_type': ['synthetic-hours']}
        initial = build_report(params)
        queries = list(ExplorerQuery.objects.filter(operation='get_cost_and_usage'))
        self.assertEqual(len(queries), 4)
        self.assertEqual(set(initial['query_ids']), {query.pk for query in queries})
        for query in queries:
            metric = query.parameters['Metrics'][0]
            selected = query.parameters['TimePeriod']['Start'] == initial['params']['start']
            amount = ('12' if selected else '8') if metric == 'UnblendedCost' else ('5' if selected else '2')
            client = Mock()
            client.get_cost_and_usage.return_value = {'ResultsByTime': [{
                'TimePeriod': query.parameters['TimePeriod'], 'Groups': [{'Keys': ['=synthetic'],
                'Metrics': {metric: {'Amount': amount, 'Unit': 'USD' if metric == 'UnblendedCost' else 'Hrs'}}}]}]}
            run_query(query, client)
            if query != queries[-1]:
                self.assertEqual(self.client.get('/export/report/', params).status_code, 409)
        result = build_report(params)
        self.assertFalse(result['report_incomplete'])
        self.assertTrue(result['comparison_ready'])
        self.assertTrue(result['usage_report']['comparison_ready'])
        exported = self.client.get('/export/report/', params)
        self.assertEqual(exported.status_code, 200)
        csv_rows = list(csv.reader(io.StringIO(exported.content.decode())))
        self.assertIn(['Comparison', 'Previous', 'Selected', 'Change', 'Change %'], csv_rows)
        self.assertIn(['Usage comparison', 'Previous', 'Selected', 'Change', 'Change %'], csv_rows)
        self.assertIn(['Total', '8', '12', '4', '50.0'], csv_rows)
        self.assertIn(['Total usage', '2', '5', '3', '150.0'], csv_rows)
        self.assertFalse(any(row and row[0] == '=synthetic' for row in csv_rows))
        self.assertTrue(any(row and row[0] == "'=synthetic" for row in csv_rows))

    def test_combined_normalized_usage_does_not_change_cost_metric(self):
        params = {'start': str(self.day), 'end': str(self.day), 'group_by': 'region',
                  'measure': 'cost_usage', 'metric': 'net_amortized', 'normalized': '1',
                  'usage_type_group': ['synthetic-instance-hours']}
        result = build_report(params)
        self.assertEqual({q.parameters['Metrics'][0] for q in ExplorerQuery.objects.all()},
                         {'NetAmortizedCost', 'NormalizedUsageAmount'})
        self.assertEqual(result['params']['normalized'], '1')
        self.assertEqual(result['usage_report']['metric_label'], 'Normalized usage')

    def test_combined_usage_rejects_mixed_returned_units(self):
        params = {'start': str(self.day), 'end': str(self.day), 'group_by': 'region',
                  'measure': 'cost_usage', 'usage_type_group': ['synthetic-mixed-units']}
        build_report(params)
        query = ExplorerQuery.objects.get(parameters__Metrics=['UsageQuantity'])
        query.data = {'ResultsByTime': [{'TimePeriod': query.parameters['TimePeriod'], 'Groups': [
            {'Keys': ['a'], 'Metrics': {'UsageQuantity': {'Amount': '1', 'Unit': 'Hrs'}}},
            {'Keys': ['b'], 'Metrics': {'UsageQuantity': {'Amount': '2', 'Unit': 'GB'}}}]}]}
        query.requested = False
        query.save()
        with self.assertRaisesMessage(ValueError, 'different units'):
            build_report(params)
        response = self.client.get('/export/report/', params)
        self.assertEqual(response.status_code, 400)
        self.assertIn(b'different units', response.content)

    def test_forecast_chart_rows_keep_interval_and_query_identity(self):
        params = {'start': str(self.day), 'end': str(self.today+timedelta(days=2)), 'group_by': 'region'}
        build_report(params)
        query = ExplorerQuery.objects.get(operation='get_cost_forecast')
        client = Mock()
        client.get_cost_forecast.return_value = {'ForecastResultsByTime': [{
            'TimePeriod': query.parameters['TimePeriod'], 'MeanValue': '12.5',
            'PredictionIntervalLowerBound': '10', 'PredictionIntervalUpperBound': '15'}]}
        run_query(query, client)
        result = build_report(params)
        payload = result['chart_payload']
        self.assertTrue(payload['forecast_requested'])
        self.assertEqual(payload['forecast_interval'], 80)
        self.assertEqual(payload['forecast_rows'], [{'series_id': str(query.pk), 'customer': self.customer.name,
            'start': str(self.today), 'end': str(self.today+timedelta(days=3)),
            'mean': 12.5, 'lower': 10.0, 'upper': 15.0}])
        self.assertTrue(result['report_incomplete'])  # forecast does not fabricate missing actuals
        self.assertEqual(payload['series'], [])

    def test_every_customer_forecast_is_required_and_retains_query_identity(self):
        make_customer('Synthetic second forecast', '222222222222')
        params = {'start': str(self.day), 'end': str(self.today+timedelta(days=2)), 'group_by': 'region'}
        initial = build_report(params)
        queries = list(ExplorerQuery.objects.order_by('operation', 'pk'))
        self.assertEqual(len(queries), 4)
        self.assertEqual(set(initial['query_ids']), {query.pk for query in queries})
        forecast_ids = {str(query.pk) for query in queries if query.operation == 'get_cost_forecast'}
        for query in queries:
            client = Mock()
            if query.operation == 'get_cost_forecast':
                client.get_cost_forecast.return_value = {'ForecastResultsByTime': [{
                    'TimePeriod': query.parameters['TimePeriod'], 'MeanValue': '12.5',
                    'PredictionIntervalLowerBound': '10', 'PredictionIntervalUpperBound': '15'}]}
            else:
                client.get_cost_and_usage.return_value = {'ResultsByTime': [{
                    'TimePeriod': query.parameters['TimePeriod'], 'Groups': [{'Keys': ['synthetic-region'],
                    'Metrics': {'UnblendedCost': {'Amount': '7', 'Unit': 'USD'}}}]}]}
            run_query(query, client)
            if query != queries[-1]:
                pending = build_report(params)
                self.assertTrue(pending['report_incomplete'])
                self.assertEqual(set(pending['query_ids']), {item.pk for item in queries})
                self.assertEqual(self.client.get('/export/report/', params).status_code, 409)
        result = build_report(params)
        self.assertFalse(result['report_incomplete'])
        self.assertFalse(result['report_pending'])
        self.assertEqual({row['series_id'] for row in result['chart_payload']['forecast_rows']}, forecast_ids)
        self.assertEqual(len(result['chart_payload']['forecast_rows']), 2)
        self.assertEqual(self.client.get('/export/report/', params).status_code, 200)

    def test_additional_filter_metadata_keeps_other_key_constraints(self):
        additional = [{'type': 'tag', 'key': 'Owner', 'values': ['alice']}]
        response = self.client.get('/explorer/metadata/', {'dimension': 'tag', 'key': 'Owner',
            'start': str(self.day), 'end': str(self.day), 'tag_key': 'Team', 'tag': ['ops'],
            'keyed_filters': json.dumps(additional)})
        self.assertEqual(response.status_code, 200)
        query = ExplorerQuery.objects.get(operation='get_tags')
        self.assertEqual(query.parameters['Filter'], {'Tags': {'Key': 'Team', 'Values': ['ops']}})
        self.assertEqual(query.parameters['TagKey'], 'Owner')

    @override_settings(REQUIRE_CONNECTION_APPROVAL=True)
    def test_additional_keys_cannot_bypass_metadata_approval(self):
        self.source.capabilities = {'tags': True}
        self.source.save()
        CustomerApproval.objects.create(customer=self.customer, status='approved', metadata=['tag:Team'])
        result = build_report({'start': str(self.day), 'end': str(self.day), 'tag_key': 'Team', 'tag': ['ops'],
            'keyed_filters': [{'type': 'tag', 'key': 'Owner', 'values': ['alice']}]})
        self.assertTrue(result['report_incomplete'])
        self.assertIn('metadata key requires customer approval', ' '.join(result['warnings']))
        self.assertFalse(ExplorerQuery.objects.exists())

    def test_absence_filters_are_expanded_in_parameter_context(self):
        result = build_report({'start': str(self.day), 'end': str(self.day), 'untagged': '1', 'tag_key': 'Team'})
        self.assertTrue(next(item for item in result['filters'] if item['key'] == 'tag')['expanded'])
