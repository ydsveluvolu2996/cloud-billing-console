"""Reject overlapping ownership even for concurrent SQL/runtime writers.

Existing conflicts intentionally block migration; an administrator must resolve
them from approved ownership evidence, never silently rewrite production history.
"""
from django.db import migrations


def install(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('CREATE EXTENSION IF NOT EXISTS btree_gist')
        schema_editor.execute('''ALTER TABLE billing_accountassignment
          ADD CONSTRAINT assignment_no_overlap EXCLUDE USING gist
          (account_id WITH =, daterange(start, "end", '[)') WITH &&)''')


def remove(apps, schema_editor):
    if schema_editor.connection.vendor == 'postgresql':
        schema_editor.execute('ALTER TABLE billing_accountassignment DROP CONSTRAINT assignment_no_overlap')


class Migration(migrations.Migration):
    dependencies = [('billing', '0018_accountassignment_metadata_and_more')]
    operations = [migrations.RunPython(install, remove)]
