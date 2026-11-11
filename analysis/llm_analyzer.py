"""LLM-based headline analysis: ticker extraction, catalyst materiality, sentiment and bias.

Supports OpenAI (JSON mode) and Anthropic (text completion parsed as JSON).
Every response is validated against ``schemas.LLMAnalysis``; malformed output is
logged and skipped, never trusted.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from typing import Protocol

import httpx
from loguru import logger
from pydantic import ValidationError

from config import MARKET_TZ, Settings
from core.resilience import retry_async
from schemas import LLMAnalysis, NewsItem

SYSTEM_PROMPT = """You are a careful financial news analyst covering companies listed on the National Stock Exchange of India (NSE).

For the single headline you receive, reply with ONLY one JSON object (no prose, no markdown) containing:

1. "ticker": the primary NSE trading symbol (uppercase, as used on NSE, e.g. "RELIANCE", "TCS", "HDFCBANK") of the listed company that the news most directly affects. Use null if no single NSE-listed company is clearly affected.
2. "is_material_catalyst": true ONLY for a concrete, price-relevant event:
   - earnings result that beats or misses expectations
   - major contract or order win or loss
   - regulatory or government approval, licence, or rejection
   - M&A, demerger, or stake sale
   - guidance change
   - buyback, dividend, or bonus announcement
   - fund raise
   - material management change
   Opinion pieces, market roundups, generic sector commentary and recycled news are NOT material.
3. "catalyst_type": one of EARNINGS, CONTRACT_WIN, REGULATORY_APPROVAL, M_AND_A, GUIDANCE, BUYBACK_DIVIDEND, FUNDRAISE, MANAGEMENT_CHANGE, OTHER, NONE. Use NONE when not material.
4. "sentiment_confidence": number from 0.0 to 1.0 for how confident you are that the event moves the stock's price in the stated direction over the next trading session. 0.0-0.3 weak or ambiguous; 0.4-0.6 moderate; 0.7-1.0 only for clear, unambiguous catalysts.
5. "directional_bias": "BULLISH", "BEARISH" or "NEUTRAL".
6. "rationale": one sentence, at most 25 words.

Rules: use only the text provided, never invent figures. If not material, set directional_bias to NEUTRAL and sentiment_confidence to at most 0.3."""

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_TRANSIENT_CLASS_NAMES = {"APIConnectionError", "APITimeoutError", "RateLimitError", "InternalServerError"}


class LLMParseError(ValueError):
    """The model returned something that is not a valid analysis object."""


class LLMClient(Protocol):
    async def complete(self, system: str, user: str) -> str: ...


class OpenAIClient:
    def __init__(self, api_key: str, model: str, timeout: float) -> None:
        from openai import AsyncOpenAI

        self._client = AsyncOpenAI(api_key=api_key, timeout=timeout, max_retries=0)
        self._model = model

    async def complete(self, system: str, user: str) -> str:
        response = await self._client.chat.completions.create(
            model=self._model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return response.choices[0].message.content or ""


class AnthropicClient:
    def __init__(self, api_key: str, model: str, timeout: float) -> None:
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=0)
        self._model = model

    async def complete(self, system: str, user: str) -> str:
        message = await self._client.messages.create(
            model=self._model,
            max_tokens=400,
            temperature=0,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return "".join(block.text for block in message.content if getattr(block, "type", "") == "text")


def build_llm_client(settings: Settings) -> LLMClient:
    key = settings.llm_api_key
    if not key:
        raise RuntimeError(f"No API key configured for LLM provider '{settings.llm_provider.value}'")
    if settings.llm_provider.value == "anthropic":
        return AnthropicClient(key, settings.llm_model, settings.llm_timeout_seconds)
    return OpenAIClient(key, settings.llm_model, settings.llm_timeout_seconds)


def build_user_prompt(item: NewsItem) -> str:
    published = item.published_at.astimezone(MARKET_TZ).isoformat() if item.published_at else "unknown"
    summary = item.summary[:800] or "(none)"
    return (
        f"Source: {item.source}\n"
        f"Published (IST): {published}\n"
        f"Headline: {item.title}\n"
        f"Summary: {summary}\n\n"
        "Return the JSON object now."
    )


def parse_llm_response(raw: str) -> LLMAnalysis:
    """Extract and validate the first JSON object in ``raw``."""
    text = _FENCE_RE.sub("", raw).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise LLMParseError("no JSON object in LLM response")
    try:
        payload = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMParseError(f"invalid JSON: {exc.msg}") from exc
    try:
        return LLMAnalysis.model_validate(payload)
    except ValidationError as exc:
        raise LLMParseError(f"schema violation: {exc.errors()[0]['msg']}") from exc


def is_transient_llm_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, asyncio.TimeoutError, ConnectionError)):
        return True
    if type(exc).__name__ in _TRANSIENT_CLASS_NAMES:
        return True
    status = getattr(exc, "status_code", None)
    return isinstance(status, int) and (status in (408, 409, 429) or status >= 500)


class LLMAnalyzer:
    def __init__(
        self,
        client: LLMClient,
        *,
        concurrency: int = 5,
        attempts: int = 3,
        retry_base_delay: float = 2.0,
    ) -> None:
        self._client = client
        self._sem = asyncio.Semaphore(concurrency)
        self._attempts = attempts
        self._retry_base_delay = retry_base_delay

    async def analyse(self, item: NewsItem) -> LLMAnalysis:
        prompt = build_user_prompt(item)
        async with self._sem:
            raw = await retry_async(
                lambda: self._client.complete(SYSTEM_PROMPT, prompt),
                attempts=self._attempts,
                base_delay=self._retry_base_delay,
                max_delay=20.0,
                retry_if=is_transient_llm_error,
                op_name="llm.complete",
            )
        return parse_llm_response(raw)

    async def analyse_batch(self, items: Sequence[NewsItem]) -> list[tuple[NewsItem, LLMAnalysis | None]]:
        """Analyse concurrently. Failed items yield ``None`` and do not abort the batch."""

        async def _one(item: NewsItem) -> tuple[NewsItem, LLMAnalysis | None]:
            try:
                return item, await self.analyse(item)
            except LLMParseError as exc:
                logger.warning("llm.unparseable", title=item.title[:120], error=str(exc))
            except Exception as exc:
                logger.error("llm.failed", title=item.title[:120], error=repr(exc))
            return item, None

        return list(await asyncio.gather(*(_one(item) for item in items)))
