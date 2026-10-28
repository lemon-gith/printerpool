"""Bulk account creation from a CSV file, with a one-time sheet of initial passwords."""

import csv
import io
from dataclasses import dataclass

from django.contrib.auth.validators import UnicodeUsernameValidator
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.functions import Lower

from .models import User, generate_initial_password

MAX_ROWS = 1000
REQUIRED_COLUMNS = {"username"}
OPTIONAL_COLUMNS = {"first_name", "last_name", "role"}
# Blank means a regular user; "student" is accepted as a friendlier name for it.
ROLE_ALIASES = {
    "": User.Role.USER,
    "user": User.Role.USER,
    "student": User.Role.USER,
    "staff": User.Role.STAFF,
    "admin": User.Role.ADMIN,
}


@dataclass
class Row:
    line: int
    username: str
    first_name: str
    last_name: str
    role: str


@dataclass
class IssuedPassword:
    user: User
    password: str


def parse_csv(uploaded_file):
    """Read and validate the whole file. Returns (rows, errors); rows are only meaningful when errors is empty."""
    try:
        text = uploaded_file.read().decode("utf-8-sig")  # -sig: tolerate the BOM Excel adds
    except UnicodeDecodeError:
        return [], ["The file isn't UTF-8 text. In Excel, use “Save as → CSV UTF-8”."]

    reader = csv.DictReader(io.StringIO(text))
    headers = {(h or "").strip().lower() for h in (reader.fieldnames or [])}
    missing = REQUIRED_COLUMNS - headers
    if missing:
        return [], [f"Missing column(s): {', '.join(sorted(missing))}. The first row must be a header row."]
    unknown = headers - REQUIRED_COLUMNS - OPTIONAL_COLUMNS
    if unknown:
        return [], [f"Unknown column(s): {', '.join(sorted(unknown))}. Allowed: username, first_name, last_name, role."]

    rows, errors, seen = [], [], {}
    validate_username = UnicodeUsernameValidator()
    for line, raw in enumerate(reader, start=2):  # line 1 is the header
        values = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        if not any(values.values()):
            continue  # blank line
        if len(rows) >= MAX_ROWS:
            errors.append(f"Too many rows: at most {MAX_ROWS} accounts per import.")
            break
        username = values.get("username", "")
        role = ROLE_ALIASES.get(values.get("role", "").lower())
        problems = []
        if not username:
            problems.append("username is empty")
        else:
            try:
                validate_username(username)
            except ValidationError:
                problems.append(f"“{username}” isn't a valid username (letters, digits and @.+-_ only)")
            if len(username) > 150:
                problems.append("username is longer than 150 characters")
            if username.lower() in seen:
                problems.append(f"“{username}” is also on line {seen[username.lower()]}")
            seen.setdefault(username.lower(), line)
        if role is None:
            problems.append(f"role “{values['role']}” isn't one of user, staff, admin")
        if problems:
            errors.append(f"Line {line}: " + "; ".join(problems))
            continue
        rows.append(Row(line, username, values.get("first_name", "")[:150], values.get("last_name", "")[:150], role))

    # Usernames are case-sensitive in Django, but "Alice" next to "alice" would only confuse people.
    existing = {
        u.lower(): u
        for u in User.objects.annotate(lower=Lower("username"))
        .filter(lower__in=[r.username.lower() for r in rows])
        .values_list("username", flat=True)
    }
    for row in rows:
        if row.username.lower() in existing:
            errors.append(f"Line {row.line}: an account called “{existing[row.username.lower()]}” already exists")
    if not rows and not errors:
        errors.append("The file has no accounts in it.")
    return rows, errors


@transaction.atomic
def create_accounts(rows):
    issued = []
    for row in rows:
        user = User(username=row.username, first_name=row.first_name, last_name=row.last_name, role=row.role)
        issued.append(issue_password(user))
    return issued


def issue_password(user):
    """Give `user` a fresh one-time password they must change at first login, and return it (never stored)."""
    password = generate_initial_password()
    user.set_password(password)
    user.must_change_password = True
    user.save()
    return IssuedPassword(user, password)
