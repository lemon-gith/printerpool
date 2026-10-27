from django.conf import settings


def contact(request):
    """Optional mailto: recipients shown in the footer (and on request pages). Empty means "don't show".
    Several addresses are joined with commas, which mailto: links accept as multiple recipients."""
    return {
        "contact_staff_email": ",".join(settings.CONTACT_STAFF_EMAILS),
        "contact_admin_email": ",".join(settings.CONTACT_ADMIN_EMAILS),
    }
