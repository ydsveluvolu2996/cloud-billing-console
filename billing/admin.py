"""Operational models use validated application workflows, not unrestricted CRUD."""
from django.contrib import admin
from django.contrib.auth.models import User, Group
# User provisioning, grants and recovery require the evidence-backed admin command.
for model in (User, Group):
    if admin.site.is_registered(model):
        admin.site.unregister(model)
