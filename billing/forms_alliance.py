from hashlib import sha256
from django import forms
from django.utils import timezone
from .models import AllianceRecord


class AllianceForm(forms.ModelForm):
    version = forms.IntegerField(min_value=0, widget=forms.HiddenInput)

    class Meta:
        model = AllianceRecord
        fields = ['account_name','legal_entity','ace_opportunity_id','bill_pulled_on','prepared_by',
                  'shared_with','date_shared','apn_marked','apn_marked_date','blocked','notes','summary_note']
        labels = {'apn_marked_date':'APN marked date'}
        widgets = {**{f:forms.DateInput(attrs={'type':'date'}) for f in ('bill_pulled_on','date_shared','apn_marked_date')},
                   'notes':forms.Textarea(attrs={'rows':3}), 'summary_note':forms.Textarea(attrs={'rows':4})}
        help_texts = {'apn_marked':'Manual confirmation of a completed Partner Central entry. This dashboard does not update APN.',
                      'blocked':'Use this for an outstanding issue; ordinary notes do not block closure.'}

    def __init__(self, *args, services=(), captured=None, revised=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.captured, self.revised = captured or {}, revised
        self.fields['version'].initial = self.instance.revision
        notes = dict(self.instance.service_notes.values_list('service','commentary')) if self.instance.pk else {}
        self.service_fields = []
        for service in services:
            name = 'service_' + sha256(service.encode()).hexdigest()[:24]
            self.fields[name] = forms.CharField(label=f'{service} — variance driver / internal commentary',
                required=False,max_length=2000,initial=notes.get(service,''),widget=forms.Textarea(attrs={'rows':2}))
            self.service_fields.append((service,name))

    def clean(self):
        data = super().clean()
        today = timezone.now().date()
        for field in ('bill_pulled_on','date_shared','apn_marked_date'):
            if data.get(field) and data[field] > today:
                self.add_error(field,'Use an actual date, not a future date.')
        pulled, shared, marked = (data.get(k) for k in ('bill_pulled_on','date_shared','apn_marked_date'))
        if shared:
            for field in ('shared_with','prepared_by','bill_pulled_on'):
                if not data.get(field):
                    self.add_error(field,'Required when recording a handoff.')
            if not self.captured:
                raise forms.ValidationError('Record the AWS figures before recording a handoff date.')
        if pulled and shared and shared < pulled:
            self.add_error('date_shared','The shared date cannot be before the bill was pulled.')
        if shared and marked and marked < shared:
            self.add_error('apn_marked_date','The APN marking date cannot be before the shared date.')
        if data.get('blocked') and not data.get('notes'):
            self.add_error('notes','Describe the outstanding blocker.')
        if marked and not data.get('apn_marked'):
            self.add_error('apn_marked_date','Clear this date when reopening the APN entry.')
        if data.get('apn_marked'):
            for field in ('legal_entity','ace_opportunity_id','prepared_by','bill_pulled_on','shared_with','date_shared','apn_marked_date'):
                if not data.get(field):
                    self.add_error(field,'Required before closing the APN entry.')
            if self.instance.month >= today.replace(day=1):
                raise forms.ValidationError('Close APN tracking only after the reporting month has ended.')
            if not self.captured or self.captured.get('state') != 'Complete' or self.revised:
                raise forms.ValidationError('APN closure requires recorded, complete, non-estimated AWS figures matching the latest import. Reopen and record the revised figures first.')
            if data.get('blocked'):
                self.add_error('blocked','Resolve the blocker before closing this entry.')
        return data
