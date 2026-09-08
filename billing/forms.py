from django import forms
from .models import Customer


class CustomerForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ['name', 'account_id', 'budget', 'currency']
        labels = {'account_id': 'AWS payer / standalone account ID', 'budget': 'Monthly budget (optional)'}
        widgets = {'name': forms.TextInput(attrs={'placeholder': 'Customer or organization name'}),
                   'account_id': forms.TextInput(attrs={'placeholder': '123456789012', 'inputmode': 'numeric'}),
                   'currency': forms.TextInput(attrs={'maxlength': 3})}


class ConnectionForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ['role_arn']
        labels = {'role_arn': 'Role ARN from CloudFormation Outputs'}
        widgets = {'role_arn': forms.TextInput(attrs={'placeholder': 'arn:aws:iam::123456789012:role/BillingConsole/CostReadOnly'})}


class BudgetForm(forms.ModelForm):
    class Meta:
        model = Customer
        fields = ['budget', 'currency']
