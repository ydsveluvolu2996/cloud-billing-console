"""
URL configuration for config project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.urls import path
from django.contrib.auth import views as auth_views
from billing import web

urlpatterns = [
    path('admin/', admin.site.urls),
    path('', web.dashboard, name='dashboard'),
    path('explorer/metadata/', web.explorer_metadata, name='explorer_metadata'),
    path('explorer/status/', web.explorer_status, name='explorer_status'),
    path('reports/save/', web.save_report, name='save_report'),
    path('reports/import/', web.import_report, name='import_report'),
    path('reports/<int:pk>/', web.open_report, name='open_report'),
    path('portfolio/', web.portfolio, name='portfolio'),
    path('export/report/', web.export_report, name='export_report'),
    path('login/', auth_views.LoginView.as_view(template_name='registration/login.html'), name='login'),
    path('logout/', auth_views.LogoutView.as_view(), name='logout'),
    path('password/', auth_views.PasswordChangeView.as_view(template_name='registration/password.html', success_url='/'), name='password_change'),
    path('customers/', web.customers, name='customers'),
    path('customers/add/', web.customer_add, name='customer_add'),
    path('customers/<uuid:pk>/', web.customer_detail, name='customer_detail'),
    path('customers/<uuid:pk>/template/', web.template_download, name='template_download'),
    path('customers/<uuid:pk>/setup/', web.launch_setup, name='launch_setup'),
    path('customers/<uuid:pk>/sync/', web.request_sync, name='request_sync'),
    path('export/', web.export_csv, name='export_csv'),
    path('refresh/', web.refresh_costs, name='refresh_costs'),
    path('activity/', web.activity, name='activity'),
    path('health/', web.health, name='health'),
]
