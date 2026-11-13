"""Tests for the read endpoints the UI uses. The repository is faked, so no database is needed."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import auth.routes as auth_routes
from auth.security import create_access_token
from config import Settings, get_settings
from db import repository as repo
from db.models import User
from server import app

SETTINGS = Settings(jwt_secret_key="z" * 48, access_token_minutes=60)


class _FakeSession:
    @asynccontextmanager
    async def begin(self):
        yield self


@pytest.fixture
def client(monkeypatch):
    user = User(id=1, email="u@example.com", password_hash="x", is_active=True)
    seen: dict = {}

    async def get_user_by_id(session, user_id):
        return user if user_id == 1 else None

    async def list_news(session, *, limit, material_only):
        seen["news"] = {"limit": limit, "material_only": material_only}
        return [
            SimpleNamespace(
                id=1,
                timestamp=datetime(2026, 11, 13, 4, 5, tzinfo=timezone.utc),
                source="currentsapi:example.com",
                title="TCS wins contract",
                url="https://example.com/tcs",
                summary="Details",
                ticker="TCS",
                sentiment_score=0.8,
                directional_bias="BULLISH",
                catalyst_type="CONTRACT_WIN",
                is_material=True,
                rationale="Clear order win",
            )
        ]

    async def load_watchlist(session, trade_date):
        seen["watch_date"] = trade_date
        return []

    async def list_trades(session, trade_date):
        seen["trade_date"] = trade_date
        return []

    async def fake_session():
        yield _FakeSession()

    monkeypatch.setattr(repo, "get_user_by_id", get_user_by_id)
    monkeypatch.setattr(repo, "list_news", list_news)
    monkeypatch.setattr(repo, "load_watchlist", load_watchlist)
    monkeypatch.setattr(repo, "list_trades", list_trades)
    app.dependency_overrides[auth_routes.get_session] = fake_session
    app.dependency_overrides[get_settings] = lambda: SETTINGS
    try:
        with TestClient(app) as test_client:
            test_client.token = create_access_token(1, "u@example.com", SETTINGS)
            test_client.seen = seen
            yield test_client
    finally:
        app.dependency_overrides.clear()


def _auth(client) -> dict:
    return {"Authorization": f"Bearer {client.token}"}


def test_endpoints_require_login(client):
    for path in ("/news", "/watchlist", "/trades"):
        assert client.get(path).status_code in (401, 403), path


def test_news_returns_items_and_passes_filters(client):
    response = client.get("/news?limit=20&material_only=true", headers=_auth(client))
    assert response.status_code == 200
    body = response.json()
    assert body[0]["ticker"] == "TCS"
    assert body[0]["is_material"] is True
    assert client.seen["news"] == {"limit": 20, "material_only": True}


def test_news_rejects_out_of_range_limit(client):
    assert client.get("/news?limit=5000", headers=_auth(client)).status_code == 422


def test_watchlist_and_trades_default_to_today_in_ist(client):
    assert client.get("/watchlist", headers=_auth(client)).status_code == 200
    assert client.get("/trades", headers=_auth(client)).status_code == 200
    assert isinstance(client.seen["watch_date"], date)
    assert client.seen["watch_date"] == client.seen["trade_date"]


def test_watchlist_accepts_explicit_date(client):
    client.get("/watchlist?trade_date=2026-11-10", headers=_auth(client))
    assert client.seen["watch_date"] == date(2026, 11, 10)
