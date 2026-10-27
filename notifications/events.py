"""The things an address can opt in to. Keys are stored in EmailAddress.notify_events, so don't rename them."""

# Sent to the person who submitted the request.
USER_EVENTS = {
    "request_approved": "My request is approved",
    "request_rejected": "My request is rejected",
    "print_started": "A print of my request starts",
    "print_succeeded": "A print of my request finishes",
    "print_failed": "A print of my request fails",
    "request_completed": "My request is marked completed",
}

# Sent to staff and admins.
STAFF_EVENTS = {
    "staff_new_request": "A new request is submitted (staff)",
}

ALL_EVENTS = {**USER_EVENTS, **STAFF_EVENTS}


def events_for(user):
    return ALL_EVENTS if user.is_print_staff else USER_EVENTS
