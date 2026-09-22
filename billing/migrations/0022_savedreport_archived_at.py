from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('billing', '0021_activationrequest')]

    operations = [
        migrations.AddField(
            model_name='savedreport',
            name='archived_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
