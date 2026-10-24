"""Authentication, sessions, CSRF and role/city authorization (all enforced server-side)."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from swarmos.config import get_settings
from swarmos.core.errors import ApiError, forbidden
from swarmos.db import get_db, utcnow
from swarmos.models import AuthSession, City, User

SESSION_COOKIE = "swarmos_session"
CSRF_COOKIE = "swarmos_csrf"
CSRF_HEADER = "x-csrf-token"
ROLE_RANK = {"viewer": 1, "operator": 2, "admin": 3}
ROLES_LIST = list(ROLE_RANK)
CLEARANCE = {"viewer": {"public"}, "operator": {"public", "internal"}, "admin": {"public", "internal", "restricted"}}
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15

_hasher = PasswordHasher()  # Argon2id with library defaults
_DUMMY_HASH = _hasher.hash("timing-equalisation-only")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def validate_password_strength(password: str) -> None:
    if len(password) < 12:
        raise ApiError(422, "weak_password", "Password must be at least 12 characters long")
    if password.lower() == password or password.upper() == password or not any(c.isdigit() for c in password):
        raise ApiError(422, "weak_password", "Password must mix upper- and lower-case letters and digits")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _as_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


@dataclass
class Principal:
    id: str
    email: str
    display_name: str
    role: str
    all_cities: bool
    city_ids: set[str] = field(default_factory=set)
    session_id: str | None = None
    csrf_token: str | None = None

    def has_role(self, role: str) -> bool:
        return ROLE_RANK[self.role] >= ROLE_RANK[role]

    def can_access_city(self, city_id: str) -> bool:
        return self.all_cities or city_id in self.city_ids

    @property
    def clearance(self) -> set[str]:
        return CLEARANCE[self.role]


def principal_from_user(user: User, session: AuthSession | None = None) -> Principal:
    return Principal(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=user.role,
        all_cities=user.all_cities,
        city_ids={uc.city_id for uc in user.cities},
        session_id=session.id if session else None,
        csrf_token=session.csrf_token if session else None,
    )


def authenticate(db: Session, email: str, password: str) -> User | None:
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    now = utcnow()
    if user is None:
        verify_password(_DUMMY_HASH, password)  # equalise timing for unknown accounts
        return None
    if user.locked_until and _as_aware(user.locked_until) > now:
        raise ApiError(423, "account_locked", "Too many failed attempts. Try again later.")
    if not user.is_active or not verify_password(user.password_hash, password):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = now + timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_logins = 0
        return None
    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = now
    if _hasher.check_needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    return user


def create_session(db: Session, user: User, request: Request, response: Response) -> AuthSession:
    settings = get_settings()
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    now = utcnow()
    db.execute(delete(AuthSession).where(AuthSession.expires_at < now))
    sess = AuthSession(
        id=_digest(token),
        user_id=user.id,
        csrf_token=csrf,
        created_at=now,
        expires_at=now + timedelta(hours=settings.session_ttl_hours),
        last_seen_at=now,
        ip=request.client.host if request.client else None,
        user_agent=(request.headers.get("user-agent") or "")[:200],
    )
    db.add(sess)
    max_age = settings.session_ttl_hours * 3600
    response.set_cookie(
        SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=bool(settings.cookie_secure),
        samesite="lax", path="/",
    )
    # Readable by JS so the SPA can echo it in the X-CSRF-Token header (double-submit pattern).
    response.set_cookie(
        CSRF_COOKIE, csrf, max_age=max_age, httponly=False, secure=bool(settings.cookie_secure),
        samesite="strict", path="/",
    )
    return sess


def destroy_session(db: Session, principal: Principal, response: Response) -> None:
    if principal.session_id:
        db.execute(delete(AuthSession).where(AuthSession.id == principal.session_id))
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def _open_access_principal(request: Request, db: Session) -> Principal:
    """The built-in account every session-less request acts as when sign-in is disabled (created on first use)."""
    email = get_settings().open_access_email.strip().lower()
    user = db.scalar(select(User).where(User.email == email))
    if user is None:
        try:
            with db.begin_nested():
                user = User(email=email, display_name="Open access", role="admin", all_cities=True,
                            password_hash=hash_password(secrets.token_urlsafe(32)))  # unusable: nobody knows it
                db.add(user)
        except IntegrityError:  # another request created it first
            user = db.scalar(select(User).where(User.email == email))
    if user is None or not user.is_active:
        raise ApiError(401, "unauthenticated", "The open-access account is disabled")
    principal = principal_from_user(user)
    request.state.principal = principal
    return principal


def current_principal(request: Request, db: Session = Depends(get_db)) -> Principal:
    token = request.cookies.get(SESSION_COOKIE)
    open_access = get_settings().auth_disabled
    if not token:
        if open_access:
            return _open_access_principal(request, db)
        raise ApiError(401, "unauthenticated", "Sign in required")
    sess = db.get(AuthSession, _digest(token))
    now = utcnow()
    if sess is None or _as_aware(sess.expires_at) < now:
        if open_access:
            return _open_access_principal(request, db)
        raise ApiError(401, "session_expired", "Your session has expired. Sign in again.")
    user = db.get(User, sess.user_id)
    if user is None or not user.is_active:
        raise ApiError(401, "unauthenticated", "Account is not active")
    if request.method not in SAFE_METHODS:
        header = request.headers.get(CSRF_HEADER, "")
        if not header or not hmac.compare_digest(header, sess.csrf_token):
            raise ApiError(403, "csrf_failed", "Missing or invalid CSRF token")
    if (now - _as_aware(sess.last_seen_at)).total_seconds() > 60:
        sess.last_seen_at = now
    principal = principal_from_user(user, sess)
    request.state.principal = principal
    return principal


def require_role(role: str):  # type: ignore[no-untyped-def]
    def dep(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.has_role(role):
            raise forbidden(f"This action requires the '{role}' role")
        return principal

    return dep


def city_access(city_id: str, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> City:
    """Validate the city identifier and the caller's scope for every city-scoped route."""
    city = db.get(City, city_id)
    if city is None or not city.is_active:
        raise ApiError(404, "unknown_city", f"Unknown city '{city_id}'")
    if not principal.can_access_city(city_id):
        raise forbidden("Your account is not authorised for this city")
    return city
