"""Authentication endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from swarmos.config import get_settings
from swarmos.core.audit import audit
from swarmos.core.errors import ApiError
from swarmos.core.ratelimit import client_ip, limiter
from swarmos.core.security import (
    Principal,
    authenticate,
    create_session,
    current_principal,
    destroy_session,
    hash_password,
    principal_from_user,
    validate_password_strength,
    verify_password,
)
from swarmos.db import get_db
from swarmos.models import User

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginIn(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=256)


class PasswordChangeIn(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(min_length=12, max_length=256)


def me_payload(p: Principal) -> dict[str, object]:
    settings = get_settings()
    return {
        "id": p.id, "email": p.email, "display_name": p.display_name, "role": p.role, "all_cities": p.all_cities,
        "city_ids": sorted(p.city_ids), "csrf_token": p.csrf_token,
        "environment": settings.env, "demo_mode": settings.demo_mode,
    }


@router.post("/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, object]:
    ip = client_ip(request)
    limiter.check(f"login:{ip}", get_settings().login_rate_limit_per_minute)
    user = authenticate(db, body.email, body.password)
    if user is None:
        audit(db, "auth.login", None, outcome="failure", details={"email": body.email.strip().lower()[:254]}, ip=ip)
        db.commit()  # persist lockout counters even though we raise
        raise ApiError(401, "invalid_credentials", "Invalid email or password")
    sess = create_session(db, user, request, response)
    p = principal_from_user(user, sess)
    audit(db, "auth.login", p, ip=ip)
    return me_payload(p)


@router.post("/logout")
def logout(response: Response, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, bool]:
    destroy_session(db, principal, response)
    audit(db, "auth.logout", principal)
    return {"ok": True}


@router.get("/me")
def me(principal: Principal = Depends(current_principal)) -> dict[str, object]:
    return me_payload(principal)


@router.post("/password")
def change_password(body: PasswordChangeIn, principal: Principal = Depends(current_principal), db: Session = Depends(get_db)) -> dict[str, bool]:
    user = db.get(User, principal.id)
    assert user is not None
    if not verify_password(user.password_hash, body.current_password):
        raise ApiError(400, "invalid_credentials", "Current password is incorrect")
    validate_password_strength(body.new_password)
    user.password_hash = hash_password(body.new_password)
    audit(db, "auth.password_change", principal)
    return {"ok": True}
