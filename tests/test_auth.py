"""Authentication tests. The user store is an in-memory fake, so no database is needed."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient

import auth.routes as auth_routes
from auth.security import create_access_token, decode_access_token, hash_password, verify_password
from config import ConfigError, Settings, get_settings
from db import repository as repo
from db.models import User
from server import app

SECRET = "x" * 48


def _settings(**overrides) -> Settings:
    base = {"jwt_secret_key": SECRET, "access_token_minutes": 60, "registration_enabled": True}
    base.update(overrides)
    return Settings(**base)


# ------------------------------------------------------------------ security helpers


def test_password_hash_verifies_and_rejects_wrong_password():
    stored = hash_password("correct horse")
    assert stored != "correct horse"
    assert verify_password("correct horse", stored)
    assert not verify_password("wrong password", stored)


def test_token_round_trip_carries_user_id_and_email():
    settings = _settings()
    claims = decode_access_token(create_access_token(7, "a@b.com", settings), settings)
    assert claims["sub"] == "7"
    assert claims["email"] == "a@b.com"


def test_expired_token_is_rejected():
    settings = _settings()
    past = datetime.now(timezone.utc) - timedelta(minutes=5)
    token = jwt.encode({"sub": "1", "exp": past}, SECRET, algorithm="HS256")
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token, settings)


def test_token_signed_with_another_key_is_rejected():
    token = jwt.encode({"sub": "1", "exp": datetime.now(timezone.utc) + timedelta(minutes=5)}, "y" * 48, algorithm="HS256")
    with pytest.raises(jwt.InvalidSignatureError):
        decode_access_token(token, _settings())


def test_tokens_are_refused_when_secret_is_missing_or_short():
    with pytest.raises(ConfigError):
        create_access_token(1, "a@b.com", _settings(jwt_secret_key="short"))


# ------------------------------------------------------------------ HTTP routes with a fake store


class _FakeSession:
    @asynccontextmanager
    async def begin(self):
        yield self


@pytest.fixture
def client(monkeypatch):
    users: dict[str, User] = {}
    counter = {"next": 1}

    async def get_user_by_email(session, email):
        return users.get(email)

    async def get_user_by_id(session, user_id):
        return next((u for u in users.values() if u.id == user_id), None)

    async def create_user(session, email, password_hash):
        user = User(id=counter["next"], email=email, password_hash=password_hash, is_active=True)
        counter["next"] += 1
        users[email] = user
        return user

    async def fake_session():
        yield _FakeSession()

    monkeypatch.setattr(repo, "get_user_by_email", get_user_by_email)
    monkeypatch.setattr(repo, "get_user_by_id", get_user_by_id)
    monkeypatch.setattr(repo, "create_user", create_user)
    app.dependency_overrides[auth_routes.get_session] = fake_session
    app.dependency_overrides[get_settings] = lambda: _settings()
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


def test_register_then_login_then_me(client):
    created = client.post("/auth/register", json={"email": "Trader@Example.com", "password": "longenough1"})
    assert created.status_code == 201
    assert created.json()["email"] == "trader@example.com"  # normalised to lower case
    assert "password" not in created.json()

    login = client.post("/auth/login", json={"email": "trader@example.com", "password": "longenough1"})
    assert login.status_code == 200
    token = login.json()["access_token"]

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json() == {"id": created.json()["id"], "email": "trader@example.com"}


def test_duplicate_registration_is_conflict(client):
    body = {"email": "dup@example.com", "password": "longenough1"}
    assert client.post("/auth/register", json=body).status_code == 201
    assert client.post("/auth/register", json=body).status_code == 409


def test_wrong_password_and_unknown_email_both_return_401(client):
    client.post("/auth/register", json={"email": "u@example.com", "password": "longenough1"})
    wrong = client.post("/auth/login", json={"email": "u@example.com", "password": "not-the-password"})
    unknown = client.post("/auth/login", json={"email": "nobody@example.com", "password": "whatever"})
    assert wrong.status_code == 401
    assert unknown.status_code == 401
    assert wrong.json() == unknown.json()  # same message, so the response does not reveal which emails exist


def test_me_without_or_with_bad_token_is_rejected(client):
    assert client.get("/auth/me").status_code in (401, 403)
    assert client.get("/auth/me", headers={"Authorization": "Bearer not-a-token"}).status_code == 401


def test_short_password_is_rejected(client):
    response = client.post("/auth/register", json={"email": "s@example.com", "password": "short"})
    assert response.status_code == 422


def test_registration_can_be_disabled(client):
    app.dependency_overrides[get_settings] = lambda: _settings(registration_enabled=False)
    response = client.post("/auth/register", json={"email": "off@example.com", "password": "longenough1"})
    assert response.status_code == 403
