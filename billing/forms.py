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
    import_budgets = forms.BooleanField(label='Import existing AWS budgets', required=False, initial=True,
        help_text='Read budgets already created in this account. This does not create or change AWS budgets.')

    class Meta:
        model = BillingSource
        fields = ['kind', 'account_id', 'shared']
        labels = {'kind': 'Account scope', 'account_id': 'AWS account ID', 'shared': 'Shared payer (accounts belong to several customers)'}
        widgets = {'account_id': forms.TextInput(attrs={'placeholder': '123456789012', 'inputmode': 'numeric'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['kind'].choices = [
            (BillingSource.STANDALONE, 'Single AWS account (including member)'),
            (BillingSource.PAYER, 'Management / payer account'),
            (BillingSource.MEMBER_BUDGETS, 'Member budget reader (no cost collection)'),
        ]
        if self.instance._state.adding:
            self.initial.setdefault('kind', BillingSource.STANDALONE)
        self.fields['kind'].help_text = 'Choose Single AWS account to connect only this account, even if it belongs to another organization.'

    def clean(self):
        data = super().clean()
        account_id = data.get('account_id')
        kind = data.get('kind')
        if data.get('shared') and kind != BillingSource.PAYER:
            self.add_error('shared', 'Shared payer applies only to a management / payer connection.')
        if account_id and kind in (BillingSource.PAYER, BillingSource.STANDALONE):
            clash = BillingSource.objects.filter(account_id=account_id, kind__in=[BillingSource.PAYER, BillingSource.STANDALONE]).exclude(pk=self.instance.pk).select_related('customer').first()
            if clash:
                raise ValidationError(f'Account {account_id} is already connected for {clash.customer.name}. Share that payer or assign its accounts instead of collecting twice.')
            member = AwsAccount.objects.filter(account_id=account_id).exclude(payer_account_id='').exclude(payer_account_id=account_id).first()
            if member and kind == BillingSource.STANDALONE:
                raise ValidationError(f'Account {account_id} is a member of organization payer {member.payer_account_id}, which is already collected. Connecting it separately would duplicate its costs.')
        return data

    def save(self, commit=True):
        source = super().save(commit=False)
        source.approved_capabilities = ['budgets'] if self.cleaned_data.get('import_budgets') or source.kind == BillingSource.MEMBER_BUDGETS else []
        if commit:
            source.save()
        return source


class ConnectionForm(forms.ModelForm):
    approved_capabilities = forms.MultipleChoiceField(label='Optional read-only data', required=False, choices=[('budgets','Existing AWS budgets'),('forecasts','AWS cost forecasts'),('organizations','Organization account inventory'),('tags','Cost allocation tags'),('cost_categories','Cost categories'),('comparison_drivers','Cost change explanations'),('resources','Resource cost details')], widget=forms.CheckboxSelectMultiple, help_text='Save your choices before creating the IAM role. The setup page combines the required permissions for the selected data.')
    class Meta:
        model = BillingSource
        fields = ['role_arn', 'approved_capabilities']
        labels = {'role_arn': 'Customer-approved IAM role ARN'}
        widgets = {'role_arn': forms.TextInput(attrs={'placeholder': 'arn:aws:iam::123456789012:role/BillingConsole/CostReadOnly'})}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['role_arn'].required = False
        if self.instance.kind != BillingSource.PAYER:
            self.fields['approved_capabilities'].choices = [(key, label) for key, label in self.fields['approved_capabilities'].choices if key != 'organizations']
        if self.instance.kind == BillingSource.MEMBER_BUDGETS:
            self.fields['approved_capabilities'].choices = [('budgets', 'Existing AWS budgets')]


class ConnectionActivationForm(forms.Form):
    contact = forms.CharField(label='Account owner or approval contact', max_length=150,
        help_text='The person authorizing access to this AWS account.')
    evidence = forms.CharField(label='Approval reference or note', max_length=500,
        widget=forms.Textarea(attrs={'rows': 2}),
        help_text='Record the request, ticket or authorization for this connection.')
    retention_days = forms.IntegerField(label='Approved retention after disconnect (days)', min_value=1, max_value=3650, initial=365,
                                       help_text='Deletion is reviewed separately after this period; existing billing history is preserved.')
    confirmed = forms.BooleanField(label='I am authorized to connect this account and import its billing data.', required=True)


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
    alias=forms.CharField(max_length=200,required=False)
    owner=forms.CharField(max_length=120,required=False)
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
