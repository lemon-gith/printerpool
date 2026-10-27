from django import forms
from django.conf import settings

from .models import Print, Printer, PrintRequest
from .validators import validate_model_file


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("widget", MultipleFileInput(attrs={"accept": ",".join(f".{e}" for e in settings.ALLOWED_MODEL_EXTENSIONS)}))
        super().__init__(*args, **kwargs)

    def clean(self, data, initial=None):
        single_clean = super().clean
        if isinstance(data, (list, tuple)):
            return [single_clean(d, initial) for d in data]
        return [single_clean(data, initial)]


class PrintRequestForm(forms.ModelForm):
    files = MultipleFileField(
        help_text=f"STL or 3MF, up to {settings.MAX_UPLOAD_MB} MB each, max {settings.MAX_FILES_PER_REQUEST} files."
    )

    class Meta:
        model = PrintRequest
        fields = ["title", "material", "colour", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 4})}

    def clean_files(self):
        files = self.cleaned_data["files"]
        if len(files) > settings.MAX_FILES_PER_REQUEST:
            raise forms.ValidationError(f"At most {settings.MAX_FILES_PER_REQUEST} files per request.")
        errors = []
        for f in files:
            try:
                validate_model_file(f)
            except forms.ValidationError as e:
                errors.extend(e.messages)
        if errors:
            raise forms.ValidationError(errors)
        return files


class RequestActionForm(forms.Form):
    to_status = forms.ChoiceField(choices=PrintRequest.Status.choices)
    note = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 2}))


class PrintCreateForm(forms.ModelForm):
    printer = forms.ModelChoiceField(queryset=Printer.objects.filter(active=True), required=False)

    class Meta:
        model = Print
        fields = ["printer", "est_duration_minutes", "est_filament_g", "priority", "notes"]
        widgets = {"notes": forms.Textarea(attrs={"rows": 2})}


class PrintActionForm(forms.Form):
    to_status = forms.ChoiceField(choices=Print.Status.choices)
    filament_used_g = forms.IntegerField(min_value=0, required=False)
    note = forms.CharField(required=False)
