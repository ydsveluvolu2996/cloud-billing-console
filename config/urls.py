"""URL configuration for the Cloud Billing Console."""
from django.urls import path, include
from django.contrib.auth import views as auth_views
from django.views.generic import RedirectView
from billing import web, authentication, views_security, user_administration
from billing import views_management as manage
from billing import views_alliance as alliance

urlpatterns = [
    path('users/', user_administration.users, name='users'),
    path('users/add/', user_administration.user_edit, name='user_add'),
    path('users/<int:pk>/', user_administration.user_edit, name='user_edit'),
    path('customers/<uuid:pk>/tree/', manage.customer_tree, name='customer_tree'),
    path('customers/<uuid:pk>/invite/', views_security.portal_invite, name='portal_invite'),
    path('portal/accept/', views_security.portal_accept, name='portal_accept'),
    path('customers/<uuid:pk>/governance/', views_security.customer_governance, name='customer_governance'),
    path('sources/<uuid:pk>/inventory/', views_security.manual_inventory, name='manual_inventory'),
    path('operations/', views_security.operations, name='operations'),
    path('security/authenticator/', authentication.change_authenticator, name='change_authenticator'),
    path('mfa/', authentication.mfa, name='mfa'),
    path('sessions/revoke/', authentication.revoke_own_sessions, name='revoke_sessions'),
    # Account provisioning and MFA recovery use evidence-backed administration;
    # do not expose third-party model administration outside those workflows.
    path('admin/', RedirectView.as_view(pattern_name='operations', permanent=False)),
    path('', web.dashboard, name='dashboard'),
    path('explorer/metadata/', web.explorer_metadata, name='explorer_metadata'),
    path('explorer/status/', web.explorer_status, name='explorer_status'),
    path('reports/save/', web.save_report, name='save_report'),
    path('reports/import/', web.import_report, name='import_report'),
    path('reports/<int:pk>/', web.open_report, name='open_report'),
    path('portfolio/', web.portfolio, name='portfolio'),
    path('overview/', manage.overview, name='overview'),
    path('alliance/', alliance.overview, name='alliance'),
    path('alliance/<uuid:customer_id>/<str:account_id>/', alliance.detail, name='alliance_detail'),
    path('export/report/', web.export_report, name='export_report'),
    path('login/', authentication.sign_in, name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('password/', auth_views.PasswordChangeView.as_view(template_name='registration/password.html', success_url='/'), name='password_change'),
    path('customers/', manage.customers, name='customers'),
    path('customers/add/', manage.customer_add, name='customer_add'),
    path('customers/<uuid:pk>/', manage.customer_detail, name='customer_detail'),
    path('customers/<uuid:pk>/edit/', manage.customer_edit, name='customer_edit'),
    path('customers/<uuid:pk>/offboard/', manage.customer_offboard, name='customer_offboard'),
    path('customers/<uuid:pk>/sync/', web.request_sync, name='request_sync'),
    path('customers/<uuid:pk>/sources/add/', manage.source_add, name='source_add'),
    path('customers/<uuid:pk>/projects/add/', manage.project_add, name='project_add'),
    path('sources/<uuid:pk>/', manage.source_detail, name='source_detail'),
    path('sources/<uuid:pk>/template/', manage.source_template, name='source_template'),
    path('sources/<uuid:pk>/setup/', manage.source_setup, name='source_setup'),
    path('sources/<uuid:pk>/<slug:action>/', manage.source_action, name='source_action'),
    path('accounts/unassigned/', manage.unassigned_accounts, name='unassigned_accounts'),
    path('accounts/<str:account_id>/', manage.account_detail, name='account_detail'),
    path('accounts/<str:account_id>/assign/', manage.account_assign, name='account_assign'),
    path('projects/<int:pk>/', manage.project_detail, name='project_detail'),
    path('projects/<int:pk>/rules/add/', manage.rule_add, name='rule_add'),
    path('projects/<int:pk>/rules/<int:rule_id>/retire/', manage.rule_retire, name='rule_retire'),
    path('budgets/', manage.budget_list, name='budget_list'),
    path('budgets/add/', manage.budget_add, name='budget_add'),
    path('budgets/bulk/', manage.budget_bulk, name='budget_bulk'),
    path('budgets/imported/', manage.imported_budgets, name='imported_budgets'),
    path('budgets/<int:pk>/', manage.budget_detail, name='budget_detail'),
    path('alerts/<int:pk>/ack/', manage.alert_ack, name='alert_ack'),
    path('onboarding/', manage.onboarding_view, name='onboarding'),
    path('onboarding/bulk/', manage.onboarding_bulk, name='onboarding_bulk'),
    path('templates/<slug:kind>.csv', manage.csv_template, name='csv_template'),
    path('export/', web.export_csv, name='export_csv'),
    path('refresh/', web.refresh_costs, name='refresh_costs'),
    path('activity/', web.activity, name='activity'),
    path('health/', web.health, name='health'),
]

from django.conf import settings
if settings.OIDC_ENABLED:
    urlpatterns += [path('oidc/', include('mozilla_django_oidc.urls'))]
