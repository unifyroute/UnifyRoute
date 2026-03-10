import httpx
from typing import Dict, Any, List
from shared.security import decrypt_secret
from .base import ProviderAdapter, ModelInfo, QuotaInfo

class _OpenAICompatAdapter(ProviderAdapter):
    """Reusable base for OpenAI-compatible APIs that expose x-ratelimit headers."""

    def __init__(self, name: str, litellm_prefix: str, base_url: str, default_tokens: int = 200_000):
        super().__init__(name, litellm_prefix)
        self._base_url = base_url
        self._default_tokens = default_tokens

    async def _list_models_impl(self, api_key: str, auth_type: str = "api_key") -> List[ModelInfo]:
        from .base import fetch_json_safe
        
        data = await fetch_json_safe(
            url=f"{self._base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout_ms=10000,
            method="GET"
        )
        
        if not data:
            return []
            
        models_data = []
        if isinstance(data, dict):
            models_data = data.get("data", data.get("models", []))
        elif isinstance(data, list):
            models_data = data
            
        return [
            ModelInfo(
                model_id=m.get("id", ""),
                display_name=m.get("id", ""),
                context_window=m.get("context_window", 32768),
                supports_functions=True,
            )
            for m in models_data
        ]

    async def _get_quota_impl(self, api_key: str, auth_type: str = "api_key") -> QuotaInfo:
        from .base import fetch_json_safe
        import httpx
        
        # Rate limits in OpenAI-compat are in the headers generally, not JSON body
        # For the pure fetch resilience, we still run the request but we need the raw headers.
        # However, OpenClaw extracts headers using raw HTTP clients wrapped in timeouts.
        # Since fetch_json_safe returns only the parsed JSON, we implement a safe header-fetch here
        # mirroring the timeout abstraction.
        try:
            timeout = httpx.Timeout(10.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.get(
                    f"{self._base_url}/models",
                    headers={"Authorization": f"Bearer {api_key}"}
                )
                if r.status_code in (401, 403):
                    return QuotaInfo(tokens_remaining=0)
                r.raise_for_status()
                x_tokens = r.headers.get("x-ratelimit-remaining-tokens", "")
                x_requests = r.headers.get("x-ratelimit-remaining-requests", "")
                
                return QuotaInfo(
                    tokens_remaining=int(x_tokens) if x_tokens.isdigit() else self._default_tokens,
                    requests_remaining=int(x_requests) if x_requests.isdigit() else 1_000,
                )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                return QuotaInfo(tokens_remaining=0)
            return QuotaInfo(tokens_remaining=self._default_tokens)
        except Exception:
            # Mirroring OpenClaw's infra gracefully swallowing failures
            return QuotaInfo(tokens_remaining=self._default_tokens)


class TogetherAdapter(_OpenAICompatAdapter):
    """Together AI (api.together.xyz) — uses together_ai/ prefix in litellm."""
    def __init__(self):
        super().__init__("together", "together_ai", "https://api.together.xyz/v1", 100_000)


class UnifyRouterAdapter(_OpenAICompatAdapter):
    """UnifyRouter (unifyroute.ai) — OpenAI-compatible, token headers present on requests."""
    def __init__(self):
        super().__init__("unifyroute", "unifyroute", "https://unifyroute.ai/api/v1", 100_000)


class PerplexityAdapter(_OpenAICompatAdapter):
    """Perplexity AI — OpenAI-compatible at api.perplexity.ai."""
    def __init__(self):
        super().__init__("perplexity", "perplexity", "https://api.perplexity.ai", 50_000)



# Known DeepSeek model pricing (input/output per 1k tokens, context window, tier).
# DeepSeek's /v1/models endpoint does NOT return cost or context info.
_DEEPSEEK_MODEL_META: dict = {
    "deepseek-chat":      {"input": 0.00027, "output": 0.00110, "ctx": 65536,  "tier": "base",     "fns": True},
    "deepseek-reasoner":  {"input": 0.00055, "output": 0.00219, "ctx": 65536,  "tier": "thinking",  "fns": False},
    "deepseek-coder":     {"input": 0.00014, "output": 0.00028, "ctx": 16000,  "tier": "base",     "fns": False},
    "deepseek-v2.5":      {"input": 0.00014, "output": 0.00028, "ctx": 128000, "tier": "base",     "fns": True},
    # Aliases / older names
    "deepseek-chat-v3":   {"input": 0.00027, "output": 0.00110, "ctx": 65536,  "tier": "base",     "fns": True},
    "deepseek-r1":        {"input": 0.00055, "output": 0.00219, "ctx": 65536,  "tier": "thinking",  "fns": False},
}
_DEEPSEEK_DEFAULT_META = {"input": 0.00027, "output": 0.00110, "ctx": 65536, "tier": "base", "fns": True}


class DeepSeekAdapter(_OpenAICompatAdapter):
    """DeepSeek — OpenAI-compatible at api.deepseek.com/v1.

    Enriches live-API model listing with pricing/tier metadata because
    DeepSeek's /v1/models endpoint does not include cost or context window.
    """
    def __init__(self):
        super().__init__("deepseek", "deepseek", "https://api.deepseek.com/v1", 100_000)

    async def _list_models_impl(self, api_key: str, auth_type: str = "api_key") -> List[ModelInfo]:
        """Fetch models from DeepSeek and enrich with known pricing."""
        from .base import fetch_json_safe
        import logging
        _log = logging.getLogger("unifyroute.adapters.deepseek")

        data = await fetch_json_safe(
            url=f"{self._base_url}/models",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout_ms=10000,
        )

        raw_models: list = []
        if isinstance(data, dict):
            raw_models = data.get("data", data.get("models", []))
        elif isinstance(data, list):
            raw_models = data

        if not raw_models:
            _log.warning("DeepSeek /v1/models returned no models (API key may be invalid or network error).")
            return []

        enriched: List[ModelInfo] = []
        for m in raw_models:
            model_id: str = m.get("id", "")
            if not model_id:
                continue

            meta = _DEEPSEEK_MODEL_META.get(model_id, _DEEPSEEK_DEFAULT_META)
            enriched.append(ModelInfo(
                model_id=model_id,
                display_name=m.get("id", model_id),
                context_window=meta["ctx"],
                input_cost_per_1k=meta["input"],
                output_cost_per_1k=meta["output"],
                supports_streaming=True,
                supports_functions=meta["fns"],
            ))
            _log.debug("DeepSeek model enriched: %s tier=%s input=%.5f output=%.5f",
                       model_id, meta["tier"], meta["input"], meta["output"])

        _log.info("DeepSeek sync: %d model(s) returned and enriched from live API.", len(enriched))
        return enriched


class CerebrasAdapter(_OpenAICompatAdapter):
    """Cerebras — OpenAI-compatible inference at api.cerebras.ai/v1."""
    def __init__(self):
        super().__init__("cerebras", "cerebras", "https://api.cerebras.ai/v1", 100_000)


class XAIAdapter(_OpenAICompatAdapter):
    """xAI Grok — OpenAI-compatible at api.x.ai/v1."""
    def __init__(self):
        super().__init__("xai", "xai", "https://api.x.ai/v1", 100_000)

class OpenRouterAdapter(_OpenAICompatAdapter):
    """OpenRouter — OpenAI-compatible at openrouter.ai/api/v1."""
    def __init__(self):
        super().__init__("openrouter", "openrouter", "https://openrouter.ai/api/v1", 200_000)
