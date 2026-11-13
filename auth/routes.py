"""/auth endpoints: register, login and the current-user lookup used to protect other routes."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from loguru import logger
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from auth.security import create_access_token, decode_access_token, hash_password, verify_password
from config import ConfigError, Settings, get_settings
from db import repository as repo
from db.models import User

router = APIRouter(prefix="/auth", tags=["auth"])
_bearer = HTTPBearer()
# Checked against when the email is unknown, so login takes about the same time either way.
_DUMMY_HASH = hash_password("timing-equaliser-not-a-real-password")


class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


async def get_session(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.session_factory() as session:
        yield session


SessionDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.post("/register", response_model=UserOut, status_code=201)
async def register(body: RegisterIn, session: SessionDep, settings: SettingsDep) -> User:
    if not settings.registration_enabled:
        raise HTTPException(status_code=403, detail="Registration is disabled")
    email = body.email.lower()
    try:
        async with session.begin():
            if await repo.get_user_by_email(session, email) is not None:
                raise HTTPException(status_code=409, detail="Email already registered")
            return await repo.create_user(session, email, hash_password(body.password))
    except IntegrityError:
        # Two identical sign-ups raced past the check above; the unique index catches the second one.
        raise HTTPException(status_code=409, detail="Email already registered") from None


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, session: SessionDep, settings: SettingsDep) -> TokenOut:
    user = await repo.get_user_by_email(session, body.email.lower())
    if user is None:
        verify_password(body.password, _DUMMY_HASH)
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if not verify_password(body.password, user.password_hash) or not user.is_active:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    try:
        token = create_access_token(user.id, user.email, settings)
    except ConfigError as exc:
        logger.error("auth.not_configured", error=str(exc))
        raise HTTPException(status_code=503, detail="Authentication is not configured on the server") from exc
    return TokenOut(access_token=token)


async def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials, Depends(_bearer)],
    session: SessionDep,
    settings: SettingsDep,
) -> User:
    unauthorised = HTTPException(
        status_code=401, detail="Invalid or expired token", headers={"WWW-Authenticate": "Bearer"}
    )
    try:
        claims = decode_access_token(credentials.credentials, settings)
    except jwt.InvalidTokenError:
        raise unauthorised from None
    except ConfigError as exc:
        raise HTTPException(status_code=503, detail="Authentication is not configured on the server") from exc
    user = await repo.get_user_by_id(session, int(claims["sub"]))
    if user is None or not user.is_active:
        raise unauthorised
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> User:
    return user
