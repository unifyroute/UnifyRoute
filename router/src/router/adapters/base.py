import logging
import litellm
import asyncio
from typing import AsyncGenerator, Dict, Any, List, Optional
from uuid import UUID
import httpx

from shared.security import decrypt_secret
from shared.models import Credential

logger = logging.getLogger(__name__)

async def fetch_json_safe(
    url: str,
    headers: dict,
    timeout_ms: int = 10000,
    method: str = "GET",
    json_body: Optional[dict] = None
) -> Optional[dict]:
    """
    Robust fetch pattern replicating OpenClaw's infra/fetch logic:
    - Enforced tight timeouts
    - Safe JSON deserialization
    - Suppresses connection errors into None rather than raising
    """
    timeout = httpx.Timeout(timeout_ms / 1000.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            kwargs = {"headers": headers}
            if json_body is not None:
                kwargs["json"] = json_body
                
            if method.upper() == "POST":
                r = await client.post(url, **kwargs)
            else:
                r = await client.get(url, **kwargs)
                
            r.raise_for_status()
            return r.json()
    except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as request_err:
        # OpenClaw connectivity gracefully swallows fetch failures at the polling layer
        # to prevent gateway crashes, returning null equivalents.
        import logging
        logging.getLogger("unifyroute.adapters").warning(f"[fetch_json_safe] {method} {url} failed: {request_err}")
        return None
    except Exception as e:
        import logging
        logging.getLogger("unifyroute.adapters").warning(f"[fetch_json_safe] {method} {url} unexpected fail: {e}")
        return None
class ModelInfo:
    """Metadata about a model returned from provider's list."""
    def __init__(self, model_id: str, display_name: str = "", context_window: int = 128000,
                 input_cost_per_1k: float = 0.0, output_cost_per_1k: float = 0.0,
                 supports_streaming: bool = True, supports_functions: bool = True):
        self.model_id = model_id
        self.display_name = display_name or model_id
        self.context_window = context_window
        self.input_cost_per_1k = input_cost_per_1k
        self.output_cost_per_1k = output_cost_per_1k
        self.supports_streaming = supports_streaming
        self.supports_functions = supports_functions


class QuotaInfo:
    """Quota / rate-limit snapshot for a credential."""
    def __init__(self, tokens_remaining: int = 0, requests_remaining: int = 0):
        self.tokens_remaining = tokens_remaining
        self.requests_remaining = requests_remaining


class ProviderAdapter:
    """Unified adapter that wraps LiteLLM for chat and provides
    list_models() / get_quota() helpers for each provider."""

    def __init__(self, provider_name: str, litellm_prefix: str | None = None):
        self.provider_name = provider_name
        # litellm_prefix is the string prepended to model IDs when calling litellm
        self.litellm_prefix = litellm_prefix or provider_name

    # ------------------------------------------------------------------
    # Core chat completion
    # ------------------------------------------------------------------
    async def chat(self, credential: Credential, messages: list, model: str,
                   stream: bool = False, **kwargs) -> Any:
        """Proxies the chat call to the provider via LiteLLM."""
        import os
        api_key = decrypt_secret(credential.secret_enc, credential.iv)
        model_str = f"{self.litellm_prefix}/{model}"
        logger.info("Adapter chat: provider=%s model=%s stream=%s", self.provider_name, model, stream)

        litellm.drop_params = True

        api_base = None
        if hasattr(credential, "provider") and credential.provider:
            pname = credential.provider.name.upper()
            if credential.provider.name == "ollama":
                default_base = "https://ollama.com"
            elif credential.provider.name == "zai":
                default_base = "https://api.z.ai/api/paas/v4"
            else:
                default_base = None
            api_base = os.environ.get(f"PROVIDER_{pname}_BASE_URL") or credential.provider.base_url or default_base

        # For OAuth2 tokens (Google Antigravity / Gemini), pass as token not api_key
        if credential.auth_type == "oauth2":
            return await litellm.acompletion(
                model=model_str,
                messages=messages,
                api_key=api_key,   # litellm accepts Bearer token as api_key for gemini
                api_base=api_base,
                stream=stream,
                **kwargs
            )

        return await litellm.acompletion(
            model=model_str,
            messages=messages,
            api_key=api_key,
            api_base=api_base,
            stream=stream,
            **kwargs
        )

    # ------------------------------------------------------------------
    # list_models — provider-specific implementations below
    # ------------------------------------------------------------------
    async def list_models(self, credential: Credential) -> List[ModelInfo]:
        """Fetch available models from the provider API."""
        api_key = decrypt_secret(credential.secret_enc, credential.iv)
        try:
            models = await self._list_models_impl(api_key, credential.auth_type)
            logger.debug("list_models: provider=%s returned %d model(s)", self.provider_name, len(models))
            return models
        except Exception as e:
            logger.warning("list_models failed for provider=%s: %s", self.provider_name, e)
            return []

    async def _list_models_impl(self, api_key: str, auth_type: str = "api_key") -> List[ModelInfo]:
        """Override in subclasses or handled by provider_name dispatch below."""
        return []

    # ------------------------------------------------------------------
    # get_quota — provider-specific implementations below
    # ------------------------------------------------------------------
    async def get_quota(self, credential: Credential) -> QuotaInfo:
        """Fetch remaining quota / rate-limit info for this credential."""
        api_key = decrypt_secret(credential.secret_enc, credential.iv)
        try:
            return await self._get_quota_impl(api_key, credential.auth_type)
        except Exception as e:
            logger.warning("get_quota failed for provider=%s: %s", self.provider_name, e)
            return QuotaInfo(tokens_remaining=10000)

    async def _get_quota_impl(self, api_key: str, auth_type: str = "api_key") -> QuotaInfo:
        return QuotaInfo(tokens_remaining=10000)


# ======================================================================
# Provider-specific subclasses
# ======================================================================
