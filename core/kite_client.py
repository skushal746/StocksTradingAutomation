"""Kite Connect helpers shared by the tick listener and order router."""

from __future__ import annotations

from collections.abc import Iterable

from kiteconnect import KiteConnect

from config import ConfigError, Settings


def build_kite_client(settings: Settings) -> KiteConnect:
    if not settings.kite_api_key:
        raise ConfigError("KITE_API_KEY is not set")
    kite = KiteConnect(api_key=settings.kite_api_key)
    token = settings.kite_access_token.get_secret_value()
    if token:
        kite.set_access_token(token)
    return kite


def resolve_instrument_tokens(kite: KiteConnect, exchange: str, symbols: Iterable[str]) -> dict[str, int]:
    """Map NSE trading symbols to instrument tokens using the daily instrument master.

    Synchronous (network call) -- invoke via ``asyncio.to_thread``.
    """
    wanted = {symbol.upper() for symbol in symbols}
    found: dict[str, int] = {}
    for row in kite.instruments(exchange):
        symbol = row.get("tradingsymbol")
        if symbol in wanted and row.get("instrument_type") == "EQ" and row.get("segment") == exchange:
            found[symbol] = int(row["instrument_token"])
    return found


def round_to_tick(price: float, tick: float = 0.05) -> float:
    """Round a price to the NSE equity tick size (Rs 0.05)."""
    return round(round(price / tick) * tick, 2)
