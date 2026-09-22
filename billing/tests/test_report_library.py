"""Report definition management, recoverable archive, and authorization boundaries."""
import json
from django.contrib.auth.models import User
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from billing.models import AuditEvent, CustomerMembership, SavedReport
from .helpers import TEST_STORAGES, make_customer, web_settings


@web_settings
class ReportLibraryTests(TestCase):
    def setUp(self):
        self.customer, _ = make_customer('Library customer', '111111111111')
        self.user = User.objects.create_user('operator', is_staff=True)
        self.client.force_login(self.user)
        self.parameters = {
            'customer': str(self.customer.pk), 'report_name': 'Monthly costs',
            'date_range': 'last_3_months', 'future_range': 'none', 'group_by': 'service', 'granularity': 'monthly',
            'service': ['Amazon EC2'], 'service_mode': 'exclude',
        }
        self.report = SavedReport.objects.create(
            customer=self.customer, name='Monthly costs', created_by=self.user.username,
            parameters=self.parameters,
        )

    def test_search_pagination_and_archive_view_are_separate(self):
        archived = SavedReport.objects.create(
            customer=self.customer, name='Archived definition', created_by=self.user.username,
            parameters={}, archived_at=timezone.now(),
        )
        response = self.client.get('/reports/')
        self.assertContains(response, 'Monthly costs')
        self.assertNotContains(response, 'Archived definition')
        self.assertEqual(response.context['active_page'], 'explorer')
        self.assertTrue(response.context['can_edit'])
        self.assertEqual(response.context['active_count'], 1)
        self.assertEqual(response.context['archived_count'], 1)
        self.assertContains(self.client.get('/reports/?status=archived'), archived.name)
        self.assertNotContains(self.client.get('/reports/?q=unmatched'), self.report.name)
        for number in range(26):
            SavedReport.objects.create(customer=self.customer, name=f'Searchable {number:02}', created_by=self.user.username)
        response = self.client.get('/reports/?q=Searchable&page=2')
        self.assertEqual(response.context['page'].paginator.count, 26)
        self.assertEqual(len(response.context['reports']), 1)
        self.assertContains(response, 'Searchable 25')
        self.assertContains(response, '?q=Searchable&amp;sort=name&amp;dir=asc&amp;page=1')
        response = self.client.get('/reports/?q=Searchable&sort=name&dir=desc&page=2')
        self.assertContains(response, 'Searchable 00')
        self.assertNotContains(response, 'Searchable 25')

    def test_duplicate_preserves_scope_settings_and_original_archive(self):
        self.report.archived_at = timezone.now()
        self.report.save(update_fields=['archived_at'])
        response = self.client.post(f'/reports/{self.report.pk}/duplicate/')
        copied = SavedReport.objects.exclude(pk=self.report.pk).get()
        self.assertRedirects(response, f'/reports/{copied.pk}/', fetch_redirect_response=False)
        self.assertEqual(copied.name, 'Monthly costs (copy)')
        self.assertEqual(copied.parameters, {**self.parameters, 'report_name': copied.name})
        self.assertEqual(copied.customer_id, self.customer.pk)
        self.assertEqual(copied.created_by, self.user.username)
        self.assertIsNone(copied.archived_at)
        self.report.refresh_from_db()
        self.assertIsNotNone(self.report.archived_at)
        self.assertEqual(self.report.parameters, self.parameters)
        self.assertEqual(AuditEvent.objects.filter(action='Explorer report duplicated').count(), 1)

    def test_metadata_columns_and_numeric_filter_sort(self):
        self.report.parameters.update(tag=['production'], keyed_filters=[{'type': 'tag', 'key': 'Owner', 'values': [], 'absent': True}])
        self.report.save(update_fields=['parameters'])
        empty = SavedReport.objects.create(customer=self.customer, name='No filters', created_by=self.user.username, parameters={})
        response = self.client.get('/reports/?sort=filter_count&dir=desc')
        self.assertEqual([row.pk for row in response.context['reports']], [self.report.pk, empty.pk])
        summary = response.context['reports'][0].summary
        self.assertEqual(summary['time_range'], 'Last 3 months')
        self.assertEqual(summary['granularity'], 'Monthly')
        self.assertEqual(summary['group_by'], 'Service')
        self.assertEqual(summary['filter_count'], 3)
        for label in ('Report name', 'Type', 'Time range', 'Time granularity', 'Grouped by', 'Filtered by'):
            self.assertContains(response, label)
        self.assertEqual(len(response.context['headers']), 6)

    def test_metadata_counts_serialized_additional_filters_and_handles_invalid_definition(self):
        self.report.parameters['keyed_filters'] = json.dumps([
            {'type': 'tag', 'key': 'Owner', 'values': ['Engineering'], 'mode': 'include'},
            {'type': 'cost_category', 'key': 'Team', 'values': [], 'absent': True},
            {'type': 'tag', 'key': 'Unused', 'values': []},
        ])
        self.report.save(update_fields=['parameters'])
        response = self.client.get('/reports/')
        self.assertEqual(response.context['reports'][0].summary['filter_count'], 3)
        self.assertEqual(set(response.context['reports'][0].summary['filter_labels'].split(', ')), {'Service', 'Tag', 'Cost category'})
        self.report.parameters['keyed_filters'] = 'invalid JSON'
        self.report.save(update_fields=['parameters'])
        self.assertContains(self.client.get('/reports/'), 'Additional filters need review.')

    def test_selected_actions_duplicate_archive_restore_and_validate_selection(self):
        second = SavedReport.objects.create(customer=self.customer, name='Second report', created_by=self.user.username, parameters={'group_by': 'region'})
        ids = [self.report.pk, second.pk]
        response = self.client.post('/reports/actions/', {'action': 'duplicate', 'report_ids': ids})
        self.assertRedirects(response, '/reports/', fetch_redirect_response=False)
        self.assertEqual(SavedReport.objects.count(), 4)
        self.client.post('/reports/actions/', {'action': 'archive', 'report_ids': ids})
        self.assertEqual(SavedReport.objects.filter(pk__in=ids, archived_at__isnull=False).count(), 2)
        self.client.post('/reports/actions/', {'action': 'restore', 'report_ids': ids})
        self.assertEqual(SavedReport.objects.filter(pk__in=ids, archived_at__isnull=True).count(), 2)
        for data in ({'action': 'archive'}, {'action': 'delete', 'report_ids': ids}, {'action': 'archive', 'report_ids': ['bad']}):
            self.assertEqual(self.client.post('/reports/actions/', data).status_code, 400)
        self.assertEqual(self.client.get('/reports/actions/').status_code, 405)

    def test_rename_updates_reopened_title_and_preserves_report_contract(self):
        response = self.client.post(f'/reports/{self.report.pk}/rename/', {'name': '  Quarterly service costs  '})
        self.assertRedirects(response, '/reports/', fetch_redirect_response=False)
        self.report.refresh_from_db()
        self.assertEqual(self.report.name, 'Quarterly service costs')
        self.assertEqual(self.report.parameters, {**self.parameters, 'report_name': 'Quarterly service costs'})
        self.assertEqual(AuditEvent.objects.filter(action='Explorer report renamed').count(), 1)

    def test_invalid_name_does_not_change_saved_definition(self):
        for name in ('', '   ', 'x' * 121, 'null\x00character'):
            with self.subTest(name=name):
                self.assertEqual(self.client.post(f'/reports/{self.report.pk}/rename/', {'name': name}).status_code, 400)
        self.report.refresh_from_db()
        self.assertEqual(self.report.name, 'Monthly costs')
        self.assertEqual(self.report.parameters, self.parameters)

    def test_archive_restore_are_idempotent_and_keep_definition(self):
        url = f'/reports/{self.report.pk}/archive/'
        self.assertRedirects(self.client.post(url), '/reports/?status=archived', fetch_redirect_response=False)
        self.report.refresh_from_db()
        first_archive = self.report.archived_at
        self.assertIsNotNone(first_archive)
        self.assertNotContains(self.client.get('/reports/'), self.report.name)
        self.assertContains(self.client.get('/reports/?status=archived'), self.report.name)
        self.assertEqual(self.client.get(f'/reports/{self.report.pk}/').status_code, 404)
        self.client.post(url)
        self.report.refresh_from_db()
        self.assertEqual(self.report.archived_at, first_archive)
        self.assertEqual(self.report.parameters, self.parameters)
        self.assertEqual(AuditEvent.objects.filter(action='Explorer report archived').count(), 1)
        self.assertRedirects(self.client.post(f'/reports/{self.report.pk}/restore/'), '/reports/', fetch_redirect_response=False)
        self.client.post(f'/reports/{self.report.pk}/restore/')
        self.report.refresh_from_db()
        self.assertIsNone(self.report.archived_at)
        self.assertEqual(self.report.parameters, self.parameters)
        self.assertEqual(AuditEvent.objects.filter(action='Explorer report restored').count(), 1)
        self.assertContains(self.client.get('/reports/'), self.report.name)
        self.assertEqual(self.client.get(f'/reports/{self.report.pk}/').status_code, 302)

    def test_rename_archived_report_keeps_it_archived(self):
        self.client.post(f'/reports/{self.report.pk}/archive/')
        response = self.client.post(f'/reports/{self.report.pk}/rename/', {'name': 'Renamed archive'})
        self.assertRedirects(response, '/reports/?status=archived', fetch_redirect_response=False)
        self.report.refresh_from_db()
        self.assertIsNotNone(self.report.archived_at)
        self.assertEqual(self.report.parameters['report_name'], 'Renamed archive')

    def test_writes_require_post_login_and_csrf(self):
        for action in ('rename', 'archive', 'restore', 'duplicate'):
            self.assertEqual(self.client.get(f'/reports/{self.report.pk}/{action}/').status_code, 405)
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.user)
        self.assertEqual(csrf_client.post(f'/reports/{self.report.pk}/archive/').status_code, 403)
        self.report.refresh_from_db()
        self.assertIsNone(self.report.archived_at)
        self.client.logout()
        self.assertEqual(self.client.get('/reports/').status_code, 302)
        self.assertEqual(self.client.post(f'/reports/{self.report.pk}/archive/').status_code, 302)


@override_settings(
    MFA_REQUIRED=False, ENFORCE_CUSTOMER_AUTHORIZATION=True,
    REQUIRE_CONNECTION_APPROVAL=False, SECURE_SSL_REDIRECT=False,
    STORAGES=TEST_STORAGES, ALLOWED_HOSTS=['testserver', 'localhost'],
)
class ReportLibraryAuthorizationTests(TestCase):
    def setUp(self):
        self.editable, _ = make_customer('Editable customer', '111111111111')
        self.readable, _ = make_customer('Readable customer', '222222222222')
        self.foreign, _ = make_customer('Foreign customer', '333333333333')
        self.user = User.objects.create_user('assigned-editor')
        self.edit_membership = CustomerMembership.objects.create(user=self.user, customer=self.editable, role='operator')
        CustomerMembership.objects.create(user=self.user, customer=self.readable, role='viewer')
        self.own = SavedReport.objects.create(customer=self.editable, name='Own editable report', created_by=self.user.username, parameters={})
        self.read_only = SavedReport.objects.create(customer=self.readable, name='Own readable report', created_by=self.user.username, parameters={})
        self.foreign_report = SavedReport.objects.create(customer=self.foreign, name='Foreign private report', created_by=self.user.username, parameters={})
        self.other_creator = SavedReport.objects.create(customer=self.editable, name='Other creator private report', created_by='another-user', parameters={})
        self.client.force_login(self.user)

    def test_library_is_customer_and_creator_scoped_with_per_row_actions(self):
        response = self.client.get('/reports/')
        self.assertContains(response, self.own.name)
        self.assertContains(response, self.read_only.name)
        self.assertNotContains(response, self.foreign_report.name)
        self.assertNotContains(response, self.other_creator.name)
        self.assertContains(response, f'/reports/{self.own.pk}/archive/')
        self.assertNotContains(response, f'/reports/{self.read_only.pk}/archive/')
        self.assertEqual(response.context['active_count'], 2)
        self.assertTrue(response.context['can_edit'])
        # An assigned operator can manage their reports without Django is_staff.
        self.assertEqual(self.client.post(f'/reports/{self.own.pk}/rename/', {'name': 'Allowed rename'}).status_code, 302)

    def test_every_mutation_rejects_foreign_creator_and_read_only_scope(self):
        for report in (self.read_only, self.foreign_report, self.other_creator):
            for action in ('rename', 'archive', 'restore', 'duplicate'):
                with self.subTest(report=report.pk, action=action):
                    self.assertEqual(self.client.post(f'/reports/{report.pk}/{action}/', {'name': 'Unauthorized rename'}).status_code, 404)
            report.refresh_from_db()
            self.assertIsNone(report.archived_at)
            self.assertNotEqual(report.name, 'Unauthorized rename')

    def test_viewer_cannot_edit_even_with_staff_flag(self):
        self.edit_membership.role = 'viewer'
        self.edit_membership.save()
        self.user.is_staff = True
        self.user.save()
        self.client.force_login(self.user)
        for action in ('rename', 'archive', 'restore', 'duplicate'):
            self.assertEqual(self.client.post(f'/reports/{self.own.pk}/{action}/', {'name': 'Denied'}).status_code, 403)
        response = self.client.get('/reports/')
        self.assertFalse(response.context['can_edit'])
        self.assertNotContains(response, f'/reports/{self.own.pk}/archive/')

    def test_mixed_unauthorized_selection_makes_no_partial_changes(self):
        before = SavedReport.objects.count()
        for action in ('archive', 'duplicate'):
            response = self.client.post('/reports/actions/', {'action': action, 'report_ids': [self.own.pk, self.foreign_report.pk]})
            self.assertEqual(response.status_code, 404)
        self.own.refresh_from_db()
        self.assertIsNone(self.own.archived_at)
        self.assertEqual(SavedReport.objects.count(), before)

    def test_revoked_scope_cannot_recover_archived_reports(self):
        self.client.post(f'/reports/{self.own.pk}/archive/')
        self.edit_membership.active = False
        self.edit_membership.save()
        # Membership changes revoke the previous session before a new login can
        # exercise the reduced authorization scope.
        self.assertRedirects(self.client.get('/reports/?status=archived'), '/login/', fetch_redirect_response=False)
        self.client.force_login(self.user)
        response = self.client.get('/reports/?status=archived')
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, self.own.name)
        self.assertEqual(self.client.post(f'/reports/{self.own.pk}/restore/').status_code, 403)
        self.own.refresh_from_db()
        self.assertIsNotNone(self.own.archived_at)
