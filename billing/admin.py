from django.contrib import admin
from . import models

for model in (models.Customer, models.BillingSource, models.AwsAccount, models.AccountAssignment, models.Project, models.AllocationRule,
              models.Budget, models.BudgetAmount, models.BudgetEvaluation, models.ImportedBudget, models.Alert, models.Job,
              models.CollectionPeriod, models.AuditEvent, models.SavedReport, models.BulkImport):
    admin.site.register(model)
