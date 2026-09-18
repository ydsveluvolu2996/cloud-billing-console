import uuid
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [('billing', '0020_bulkimport_requested_by_bulkimport_scope_fingerprint'),
                    migrations.swappable_dependency(settings.AUTH_USER_MODEL)]
    operations = [
        migrations.AlterField(model_name='billingsource', name='kind', field=models.CharField(
            choices=[('payer','Management / payer account'),('standalone','Single AWS account (including member)'),
                     ('member_budgets','Member budget reader (no cost collection)')], default='payer', max_length=20)),
        migrations.CreateModel(name='ActivationRequest', fields=[
            ('id', models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
            ('session_version', models.PositiveIntegerField()),
            ('connection_version', models.PositiveIntegerField()),
            ('snapshot', models.JSONField()),
            ('status', models.CharField(choices=[('queued','Waiting to connect'),('processing','Authorizing connection'),('verifying','Checking AWS access'),('importing','Importing billing history'),('completed','Connected'),('failed','Needs attention')], default='queued', max_length=20)),
            ('attempts', models.PositiveIntegerField(default=0)),
            ('last_error', models.CharField(blank=True, max_length=500)),
            ('created_at', models.DateTimeField(auto_now_add=True)),
            ('updated_at', models.DateTimeField(auto_now=True)),
            ('activated_at', models.DateTimeField(blank=True, null=True)),
            ('finished_at', models.DateTimeField(blank=True, null=True)),
            ('requested_by', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL)),
            ('source', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='activation_requests', to='billing.billingsource')),
        ], options={'ordering':['-created_at']}),
        migrations.AddConstraint(model_name='activationrequest', constraint=models.UniqueConstraint(fields=('source',), condition=models.Q(status__in=['queued','processing','verifying','importing']), name='unique_active_activation_source')),
    ]
