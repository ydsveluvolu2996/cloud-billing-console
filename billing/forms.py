import re
from datetime import date
from decimal import Decimal
from django import forms
from django.core.exceptions import ValidationError
from .models import AllocationRule, AwsAccount, BillingSource, Budget, Customer, Project

ACCOUNT_RE = re.compile(r'^\d{12}$')


def parse_accounts(text):
    ids = sorted({x.strip() for x in re.split(r'[\s,;]+', text or '') if x.strip()})
    bad = [x for x in ids if not ACCOUNT_RE.match(x)]
    if bad:
        raise ValidationError(f'Invalid AWS account ID(s): {", ".join(bad)}')
    return ids


class CustomerForm(forms.ModelForm):
    budget = forms.DecimalField(label='Monthly customer budget (optional)', required=False, min_value=Decimal('0.01'), decimal_places=2)

    class Meta:
        model = Customer
        fields = ['name', 'reference', 'owner', 'currency']
        widgets = {'name': forms.TextInput(attrs={'placeholder': 'Customer or organization name'}),
                   'reference': forms.TextInput(attrs={'placeholder': 'CRM / contract reference'}),
                   'owner': forms.TextInput(attrs={'placeholder': 'Internal account owner'}),
                   'currency': forms.TextInput(attrs={'maxlength': 3})}


class SourceForm(forms.ModelForm):
    class Meta:
        model = BillingSource
        fields = ['kind', 'account_id', 'shared']
        labels = {'account_id': 'AWS account ID', 'shared': 'Shared payer (accounts belong to several customers)'}
        widgets = {'account_id': forms.TextInput(attrs={'placeholder': '123456789012', 'inputmode': 'numeric'})}

    def clean(self):
        data = super().clean()
        account_id = data.get('account_id')
        kind = data.get('kind')
        if account_id and kind in (BillingSource.PAYER, BillingSource.STANDALONE):
            clash = BillingSource.objects.filter(account_id=account_id, kind__in=[BillingSource.PAYER, BillingSource.STANDALONE]).exclude(pk=self.instance.pk).select_related('customer').first()
            if clash:
                raise ValidationError(f'Account {account_id} is already connected for {clash.customer.name}. Share that payer or assign its accounts instead of collecting twice.')
            member = AwsAccount.objects.filter(account_id=account_id).exclude(payer_account_id='').exclude(payer_account_id=account_id).first()
            if member and kind == BillingSource.STANDALONE:
                raise ValidationError(f'Account {account_id} is a member of organization payer {member.payer_account_id}, which is already collected. Connecting it separately would duplicate its costs.')
        return data


class ConnectionForm(forms.ModelForm):
    class Meta:
        model = BillingSource
        fields = ['role_arn']
        labels = {'role_arn': 'Customer-approved IAM role ARN'}
        widgets = {'role_arn': forms.TextInput(attrs={'placeholder': 'arn:aws:iam::123456789012:role/BillingConsole/CostReadOnly'})}


class BudgetForm(forms.ModelForm):
    amount = forms.DecimalField(label='Monthly limit', min_value=Decimal('0.01'), decimal_places=2)
    effective_from = forms.DateField(initial=lambda: date.today().replace(day=1), help_text='First month this limit applies.')
    include_services = forms.CharField(required=False, help_text='Optional comma-separated AWS service names to include.')
    exclude_services = forms.CharField(required=False, help_text='Optional comma-separated AWS service names to exclude.')

    class Meta:
        model = Budget
        fields = ['owner', 'name', 'scope', 'source', 'account_id', 'project', 'currency', 'metric', 'actual_threshold', 'forecast_threshold']

    def __init__(self, *args, customer=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.customer = customer or (self.instance.customer if self.instance.pk else None)
        if self.customer:
            self.fields['source'].queryset = BillingSource.objects.filter(customer=self.customer).exclude(kind=BillingSource.MEMBER_BUDGETS)
            self.fields['project'].queryset = Project.objects.filter(customer=self.customer)
        for name in ('source', 'project'):
            self.fields[name].required = False
        if self.instance.pk:
            self.fields['include_services'].initial = ', '.join(self.instance.filters.get('include_services', []))
            self.fields['exclude_services'].initial = ', '.join(self.instance.filters.get('exclude_services', []))

    def clean(self):
        data = super().clean()
        if data.get('account_id') and not ACCOUNT_RE.match(data['account_id']):
            self.add_error('account_id', 'Enter a 12-digit AWS account ID.')
        scope = data.get('scope')
        if scope == Budget.ACCOUNT and data.get('account_id') and self.customer:
            if not self.customer.assignments.filter(account__account_id=data['account_id'], end__isnull=True).exists():
                self.add_error('account_id', 'This account is not currently assigned to the customer.')
        return data

    def save(self, commit=True):
        budget = super().save(commit=False)
        budget.customer = self.customer
        budget.filters = {k: [s.strip() for s in (self.cleaned_data.get(k) or '').split(',') if s.strip()] for k in ('include_services', 'exclude_services')}
        budget.filters = {k: v for k, v in budget.filters.items() if v}
        budget.full_clean(exclude=['created_by'])
        if commit:
            budget.save()
        return budget


class BudgetOverrideForm(forms.Form):
    month = forms.DateField(help_text='Any date inside the month; the first day is stored.')
    amount = forms.DecimalField(min_value=Decimal('0.01'), decimal_places=2)


class ProjectForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ['name', 'code', 'description', 'active']


class AllocationRuleForm(forms.ModelForm):
    accounts = forms.CharField(required=False, widget=forms.Textarea(attrs={'rows': 2}), help_text='Linked account IDs separated by commas or new lines.')
    values_text = forms.CharField(label='Tag / category values', required=False, help_text='Comma separated, e.g. ERP, erp-prod')

    class Meta:
        model = AllocationRule
        fields = ['kind', 'key', 'priority', 'effective_start', 'effective_end']

    def clean(self):
        data = super().clean()
        kind = data.get('kind')
        data['account_ids'] = parse_accounts(data.get('accounts', ''))
        data['values'] = sorted({v.strip() for v in (data.get('values_text') or '').split(',') if v.strip()})
        if kind in (AllocationRule.ACCOUNTS, AllocationRule.ACCOUNT_TAG, AllocationRule.ACCOUNT_CATEGORY) and not data['account_ids']:
            self.add_error('accounts', 'Choose at least one linked account.')
        if kind != AllocationRule.ACCOUNTS:
            if not data.get('key'):
                self.add_error('key', 'Enter the activated tag key or cost category name.')
            if not data['values']:
                self.add_error('values_text', 'Enter at least one value.')
        if data.get('effective_end') and data.get('effective_start') and data['effective_end'] <= data['effective_start']:
            self.add_error('effective_end', 'Effective end must be after the start.')
        return data

    def save(self, commit=True):
        rule = super().save(commit=False)
        rule.account_ids = self.cleaned_data['account_ids']
        rule.values = self.cleaned_data['values']
        if commit:
            rule.save()
        return rule


class AssignmentForm(forms.Form):
    customer = forms.ModelChoiceField(queryset=Customer.objects.filter(active=True))
    start = forms.DateField(initial=lambda: date.today().replace(day=1), help_text='First day the customer owns this account’s spend.')
    environment = forms.ChoiceField(choices=[('', 'Unlabelled'), ('production', 'Production'), ('development', 'Development'), ('other', 'Other')], required=False)
    note = forms.CharField(max_length=200, required=False)


class CsvUploadForm(forms.Form):
    file = forms.FileField(label='CSV file')

    def clean_file(self):
        f = self.cleaned_data['file']
        if f.size > 2 * 1024 * 1024:
            raise ValidationError('CSV files are limited to 2 MB.')
        return f
