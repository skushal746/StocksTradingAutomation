import asyncio
from datetime import datetime, timezone

import httpx
import pytest

from analysis.llm_analyzer import LLMAnalyzer, LLMParseError, parse_llm_response
from schemas import Bias, CatalystType, NewsItem

VALID = """```json
{"ticker": "nse:tcs", "is_material_catalyst": true, "catalyst_type": "CONTRACT_WIN",
 "sentiment_confidence": 0.82, "directional_bias": "BULLISH", "rationale": "Large deal win."}
```"""


def test_parses_fenced_json_and_normalises_ticker():
    analysis = parse_llm_response(VALID)
    assert analysis.ticker == "TCS"
    assert analysis.catalyst_type is CatalystType.CONTRACT_WIN
    assert analysis.directional_bias is Bias.BULLISH
    assert analysis.sentiment_confidence == pytest.approx(0.82)


def test_invalid_ticker_becomes_none():
    raw = '{"ticker": "NOT A TICKER!!", "is_material_catalyst": false, "sentiment_confidence": 0.1, "directional_bias": "NEUTRAL"}'
    assert parse_llm_response(raw).ticker is None


def test_confidence_out_of_range_is_rejected():
    raw = '{"ticker": "TCS", "is_material_catalyst": true, "sentiment_confidence": 1.7, "directional_bias": "BULLISH"}'
    with pytest.raises(LLMParseError):
        parse_llm_response(raw)


def test_missing_json_is_rejected():
    with pytest.raises(LLMParseError):
        parse_llm_response("I cannot help with that.")


def test_bad_bias_is_rejected():
    raw = '{"ticker": "TCS", "is_material_catalyst": true, "sentiment_confidence": 0.5, "directional_bias": "UP"}'
    with pytest.raises(LLMParseError):
        parse_llm_response(raw)


class _FlakyClient:
    """Fails once with a transient transport error, then returns a valid analysis."""

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, system: str, user: str) -> str:
        self.calls += 1
        if self.calls == 1:
            raise httpx.ConnectError("boom")
        return VALID


def test_analyser_retries_transient_errors_then_succeeds():
    item = NewsItem(
        source="s",
        title="TCS wins deal",
        fetched_at=datetime.now(timezone.utc),
        dedupe_key="k",
    )
    client = _FlakyClient()
    analyser = LLMAnalyzer(client, retry_base_delay=0.01)
    analysis = asyncio.run(analyser.analyse(item))
    assert analysis.ticker == "TCS"
    assert client.calls == 2


def test_analyse_batch_skips_unparseable_without_failing_batch():
    class _Garbage:
        async def complete(self, system: str, user: str) -> str:
            return "not json"

    item = NewsItem(source="s", title="x", fetched_at=datetime.now(timezone.utc), dedupe_key="k")
    results = asyncio.run(LLMAnalyzer(_Garbage()).analyse_batch([item]))
    assert results == [(item, None)]
