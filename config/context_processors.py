from django.conf import settings


def contact(request):
    """Optional mailto: contacts shown in the footer (and on request pages). Empty means "don't show"."""
    return {
        "contact_staff_email": settings.CONTACT_STAFF_EMAIL,
        "contact_admin_email": settings.CONTACT_ADMIN_EMAIL,
    }
