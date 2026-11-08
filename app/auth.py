"""Owner accounts: password hashing, login sessions, and the require_owner dependency."""

import re

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Owner

_hasher = PasswordHasher()
# Verified against when the username doesn't exist, so a wrong username takes as long
# as a wrong password and doesn't reveal which usernames exist.
_DUMMY_HASH = _hasher.hash("not-a-real-password")

USERNAME_RE = re.compile(r"^[a-z0-9_.-]{3,64}$")
RESERVED_USERNAMES = {"auto"}  # "auto" marks automatic batches in generation_batches.requested_by
MIN_PASSWORD_LENGTH = 10

SESSION_KEY = "owner_id"


class OwnerError(ValueError):
    pass


class NotLoggedIn(Exception):
    """Raised by require_owner; app/main.py turns it into a redirect to the login page."""


def normalize_username(username: str) -> str:
    return username.strip().lower()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def _check_password(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise OwnerError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")


def owners_exist(db: Session) -> bool:
    return db.scalar(select(Owner.id).limit(1)) is not None


def create_owner(db: Session, username: str, password: str) -> Owner:
    username = normalize_username(username)
    if not USERNAME_RE.match(username):
        raise OwnerError("Username must be 3-64 characters: a-z, 0-9, _ . -")
    if username in RESERVED_USERNAMES:
        raise OwnerError(f'"{username}" is reserved')
    _check_password(password)
    if db.scalar(select(Owner).where(Owner.username == username)):
        raise OwnerError(f'Owner "{username}" already exists')
    owner = Owner(username=username, password_hash=hash_password(password))
    db.add(owner)
    db.commit()
    return owner


def set_password(db: Session, username: str, password: str) -> None:
    owner = db.scalar(select(Owner).where(Owner.username == normalize_username(username)))
    if owner is None:
        raise OwnerError(f'No owner named "{username}"')
    _check_password(password)
    owner.password_hash = hash_password(password)
    db.commit()


def change_password(db: Session, owner: Owner, current: str, new: str, repeat: str) -> None:
    """Change a logged-in owner's own password. Raises OwnerError with a message for the form."""
    try:
        _hasher.verify(owner.password_hash, current)
    except (VerifyMismatchError, InvalidHashError):
        raise OwnerError("Your current password is wrong.") from None
    if new != repeat:
        raise OwnerError("The new passwords don't match.")
    _check_password(new)
    owner.password_hash = hash_password(new)
    db.commit()


def authenticate(db: Session, username: str, password: str) -> Owner | None:
    owner = db.scalar(select(Owner).where(Owner.username == normalize_username(username)))
    try:
        _hasher.verify(owner.password_hash if owner else _DUMMY_HASH, password)
    except (VerifyMismatchError, InvalidHashError):
        return None
    if owner is None:
        return None
    if _hasher.check_needs_rehash(owner.password_hash):
        owner.password_hash = hash_password(password)
        db.commit()
    return owner


def login(request: Request, owner: Owner) -> None:
    request.session.clear()
    request.session[SESSION_KEY] = owner.id


def logout(request: Request) -> None:
    request.session.clear()


def require_owner(request: Request, db: Session = Depends(get_db)) -> Owner:
    owner_id = request.session.get(SESSION_KEY)
    owner = db.get(Owner, owner_id) if owner_id is not None else None
    if owner is None:
        # Not logged in, or the account was deleted since.
        request.session.clear()
        raise NotLoggedIn()
    return owner
