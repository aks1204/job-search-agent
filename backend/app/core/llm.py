"""The single doorway to the Anthropic API.

- Two tiers: "fast" (cheap extraction, Claude Haiku 5.5) and "judge" (verdicts, Claude Sonnet 5.5).
- Structured outputs: every call returns a validated Pydantic object or raises LLMError.
- The system prompt is marked cacheable (it's the stable part: rules, owner profile).
- Every call is recorded in llm_calls with tokens and cost.
"""

import copy
import json
import logging
from dataclasses import dataclass
from typing import Generic, Literal, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.config_loader import get_config
from app.models.llm_call import LLMCall
from app.settings import get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
Tier = Literal["fast", "judge"]

FALLBACK_BETA = "server-side-fallback-2026-07-01"


class LLMError(RuntimeError):
    pass


class LLMUnavailable(LLMError):
    """No API key configured."""


@dataclass
class LLMResult(Generic[T]):
    parsed: T
    model: str
    cost_usd: float
    input_tokens: int
    output_tokens: int


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema → structured-outputs schema: refs inlined, every field required, no extras."""
    schema = model.model_json_schema()
    defs = schema.pop("$defs", {})

    def walk(node):
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            return walk(copy.deepcopy(defs[node["$ref"].rsplit("/", 1)[-1]]))
        out = {k: walk(v) for k, v in node.items() if k not in ("title", "default")}
        if out.get("type") == "object" and "properties" in out:
            out["additionalProperties"] = False
            out["required"] = list(out["properties"])
        return out

    return walk(schema)


def _cost(model: str, inp: int, out: int, cache_read: int, cache_write: int) -> float:
    prices = get_config().llm.prices
    rates = prices.get(model) or next((v for k, v in prices.items() if model.startswith(k)), None)
    if not rates or len(rates) < 4:
        log.warning("no price configured for model %s; cost recorded as 0", model)
        return 0.0
    p_in, p_out, p_read, p_write = rates[:4]
    return (inp * p_in + out * p_out + cache_read * p_read + cache_write * p_write) / 1_000_000


class LLM:
    def __init__(self, client: anthropic.Anthropic | None = None):
        if client is None:
            key = get_settings().anthropic_api_key
            if not key:
                raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
            client = anthropic.Anthropic(api_key=key, max_retries=3, timeout=180.0)
        self._client = client

    @staticmethod
    def available() -> bool:
        return bool(get_settings().anthropic_api_key)

    def _send(self, tier: Tier, kwargs: dict):
        cfg = get_config().llm
        if tier == "judge" and cfg.server_side_fallback:
            try:
                return self._client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            except TypeError:
                # Older SDK without a typed `fallbacks` argument: send it as a raw body field.
                return self._client.beta.messages.create(
                    betas=[FALLBACK_BETA], extra_body={"fallbacks": "default"}, **kwargs
                )
        return self._client.messages.create(**kwargs)

    def structured(
        self,
        tier: Tier,
        *,
        system: str,
        user: str,
        schema: type[T],
        purpose: str,
        session: Session | None = None,
        subject: tuple[str, str] | None = None,
    ) -> LLMResult[T]:
        settings, tier_cfg = get_settings(), getattr(get_config().llm, tier)
        model = settings.llm_fast_model if tier == "fast" else settings.llm_judge_model
        kwargs = {
            "model": model,
            "max_tokens": tier_cfg.max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": tier_cfg.effort,
                "format": {"type": "json_schema", "schema": strict_schema(schema)},
            },
        }
        try:
            response = self._send(tier, kwargs)
        except anthropic.APIError as e:
            raise LLMError(f"{purpose}: {type(e).__name__}: {e}") from e

        usage = response.usage
        inp, out = usage.input_tokens or 0, usage.output_tokens or 0
        c_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        c_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        served_by = getattr(response, "model", None) or model
        cost = _cost(served_by, inp, out, c_read, c_write)
        refused = response.stop_reason == "refusal"

        if session is not None:
            session.add(LLMCall(
                purpose=purpose, model=served_by, input_tokens=inp, output_tokens=out,
                cache_read_tokens=c_read, cache_write_tokens=c_write, cost_usd=cost, refused=refused,
                subject_type=subject[0] if subject else None, subject_id=subject[1] if subject else None,
                request_id=getattr(response, "_request_id", None),
            ))

        if refused:
            raise LLMError(f"{purpose}: model declined the request")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"{purpose}: response hit max_tokens ({tier_cfg.max_tokens}); raise it in config/llm.yaml")
        text = next((b.text for b in response.content if b.type == "text"), None)
        if text is None:
            raise LLMError(f"{purpose}: no text block in response")
        try:
            parsed = schema.model_validate(json.loads(text))
        except (json.JSONDecodeError, ValidationError) as e:
            raise LLMError(f"{purpose}: response did not match schema: {e}") from e
        return LLMResult(parsed=parsed, model=served_by, cost_usd=cost, input_tokens=inp, output_tokens=out)
