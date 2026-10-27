from django import forms

from .events import events_for


class AddEmailForm(forms.Form):
    email = forms.EmailField(label="Email address")


class VerifyCodeForm(forms.Form):
    code = forms.RegexField(
        regex=r"^\s*\d{6}\s*$",
        label="6-digit code",
        error_messages={"invalid": "Enter the 6-digit code from the email."},
        widget=forms.TextInput(attrs={"inputmode": "numeric", "autocomplete": "one-time-code", "size": 8}),
    )


class PreferencesForm(forms.Form):
    events = forms.MultipleChoiceField(required=False, widget=forms.CheckboxSelectMultiple, label="Email me when")

    def __init__(self, *args, address, **kwargs):
        kwargs.setdefault("initial", {"events": address.notify_events})
        kwargs.setdefault("auto_id", f"id_%s_{address.pk}")  # several of these forms share one page
        super().__init__(*args, **kwargs)
        self.fields["events"].choices = list(events_for(address.user).items())
