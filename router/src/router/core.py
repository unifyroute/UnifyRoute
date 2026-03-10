import logging
from uuid import UUID
from typing import Tuple, List, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
import hashlib

from shared.models import Credential, ProviderModel
from shared.schemas import ChatRequest
from router.config import get_routing_config
from router.quota import get_quota_for_model, is_provider_failed, mark_provider_failed, get_redis

logger = logging.getLogger(__name__)


class Candidate:
    def __init__(
        self,
        credential_id: UUID,
        provider: str,
        model_id: str,
        cost: float,
        quota: int,
        input_cost_per_1k: float = 0.0,
        output_cost_per_1k: float = 0.0,
    ):
        self.credential_id = credential_id
        self.provider = provider
        self.model_id = model_id
        self.cost = cost  # combined = input + output per 1k, used for sorting
        self.quota = quota
        self.input_cost_per_1k = input_cost_per_1k
        self.output_cost_per_1k = output_cost_per_1k


async def get_candidate_details(session: AsyncSession, provider_name: str, model_id: str, needs_functions: bool = False, diagnostics: dict = None) -> List[Candidate]:
    """Look up credentials and models for a given provider + model_id string."""
    logger.debug("Looking up candidates for provider=%s model=%s", provider_name, model_id)
    stmt = (
        select(Credential, ProviderModel)
        .join(ProviderModel, Credential.provider_id == ProviderModel.provider_id)
        .where(
            ProviderModel.model_id == model_id,
            ProviderModel.provider.has(name=provider_name)
        )
    )
    result = await session.execute(stmt)

    candidates = []
    for cred, model in result:
        if diagnostics is not None:
            diagnostics["total"] += 1

        if not cred.enabled or not model.enabled:
            if diagnostics is not None:
                diagnostics.setdefault("disabled", 0)
                diagnostics["disabled"] += 1
            continue

        # Check fail state
        if await is_provider_failed(cred.id, model.model_id):
            if diagnostics is not None:
                diagnostics["failed"] += 1
            continue
            
        # Check function calling capabilities
        if needs_functions and not model.supports_functions:
            if diagnostics is not None:
                diagnostics["no_tools"] += 1
            continue

        # Get quota
        quota = await get_quota_for_model(cred.id, model.model_id)
        if quota is None:
            # Fallback quota if redis doesn't know, treat as a high number
            quota = 999999999

        candidates.append(Candidate(
            credential_id=cred.id,
            provider=provider_name,
            model_id=model.model_id,
            cost=float(model.input_cost_per_1k) + float(model.output_cost_per_1k),
            quota=quota,
            input_cost_per_1k=float(model.input_cost_per_1k),
            output_cost_per_1k=float(model.output_cost_per_1k),
        ))

    logger.debug("get_candidate_details: provider=%s model=%s → %d candidate(s)", provider_name, model_id, len(candidates))
    return candidates


import re as _re

# ── Task-type keyword patterns (compiled once) ───────────────────────────
_CODE_PATTERNS = _re.compile(
    r"\b(code|function|debug|implement|refactor|python|javascript|typescript|java|"
    r"rust|golang|sql|api|bug|compile|syntax|algorithm|class|method|variable|"
    r"import|library|package|deploy|dockerfile|yaml|json|regex)\b|```",
    _re.IGNORECASE,
)
_ANALYSIS_PATTERNS = _re.compile(
    r"\b(analyze|analysis|compare|explain|summarize|summary|research|evaluate|"
    r"assess|interpret|investigate|statistics|data|insight|reason|logic|math|"
    r"calculate|derive|prove|theorem)\b",
    _re.IGNORECASE,
)
_CREATIVE_PATTERNS = _re.compile(
    r"\b(write|story|poem|creative|draft|blog|essay|novel|script|compose|"
    r"imagine|fiction|narrative|lyric|song|copywriting|brainstorm)\b",
    _re.IGNORECASE,
)
_TRANSLATION_PATTERNS = _re.compile(
    r"\b(translate|translation|spanish|french|german|chinese|japanese|korean|"
    r"arabic|hindi|portuguese|italian|russian|dutch|turkish|polish|"
    r"multilingual|localize|localization)\b",
    _re.IGNORECASE,
)


def _detect_task_type(request: ChatRequest) -> str:
    """Classify the request's primary task type from the last user message."""
    # Use the last user message for classification
    last_user = ""
    for m in reversed(request.messages):
        if m.role == "user":
            content = m.content
            if isinstance(content, str):
                last_user = content
            elif isinstance(content, list):
                # Multimodal or complex content blocks
                text_parts = []
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                last_user = "\n".join(text_parts)
            break
            
    if not last_user:
        return "simple"

    # Score each pattern
    scores = {
        "code": len(_CODE_PATTERNS.findall(last_user)),
        "analysis": len(_ANALYSIS_PATTERNS.findall(last_user)),
        "creative": len(_CREATIVE_PATTERNS.findall(last_user)),
        "translation": len(_TRANSLATION_PATTERNS.findall(last_user)),
    }

    best = max(scores, key=lambda k: scores[k])
    if scores[best] == 0:
        return "simple"
    return best


# Map task type → (preferred tier, optional model name substring patterns)
_TASK_TIER_MAP = {
    "code":        ("thinking", ["coder", "code", "deepseek"]),
    "analysis":    ("thinking", []),
    "creative":    ("base", []),
    "translation": ("base", []),
    "simple":      ("lite", []),
}


def _auto_select_tier(request: ChatRequest) -> str:
    """Heuristic-based tier selection for the 'auto' virtual alias.

    Combines task-type detection with size heuristics.
    """
    # 1. Task-type classification
    task_type = _detect_task_type(request)
    preferred_tier, _ = _TASK_TIER_MAP.get(task_type, ("lite", []))
    logger.debug("Auto-tier: task_type=%s preferred_tier=%s", task_type, preferred_tier)

    # 2. Size override: very long requests should always use thinking
    total_content_len = 0
    for m in request.messages:
        if isinstance(m.content, str):
            total_content_len += len(m.content)
        elif isinstance(m.content, list):
            for part in m.content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total_content_len += len(part.get("text", ""))

    msg_count = len(request.messages)
    max_tokens = request.max_tokens or 0

    if max_tokens > 4096 or msg_count > 10 or total_content_len > 3000:
        return "thinking"
    if max_tokens > 512 or msg_count > 3 or total_content_len > 500:
        # Size says base or higher — pick whichever is the richer tier
        if preferred_tier == "lite":
            logger.info("Auto-tier selected: base (size override from lite)")
            return "base"

    logger.info("Auto-tier selected: %s (task_type=%s)", preferred_tier, task_type)
    return preferred_tier


async def _try_task_specific_models(
    session: AsyncSession, request: ChatRequest, diagnostics: dict = None
) -> Optional[List["Candidate"]]:
    """Try to find candidates that match the task type's preferred model patterns."""
    task_type = _detect_task_type(request)
    _, model_patterns = _TASK_TIER_MAP.get(task_type, ("lite", []))
    if not model_patterns:
        return None  # No specific model preference — fall through to tier routing

    from shared.models import Provider
    
    needs_functions = bool(getattr(request, "model_extra", {}) and request.model_extra.get("tools"))
    
    # Look for any enabled model whose model_id contains one of the preferred patterns
    stmt = (
        select(Credential, ProviderModel, Provider.name)
        .join(ProviderModel, Credential.provider_id == ProviderModel.provider_id)
        .join(Provider, Credential.provider_id == Provider.id)
        .where(
            Provider.enabled == True
        )
    )
    result = await session.execute(stmt)

    candidates_list = []
    for cred, model, provider_name in result:
        model_lower = model.model_id.lower()
        if not any(pat in model_lower for pat in model_patterns):
            continue
            
        if diagnostics is not None:
            diagnostics["total"] += 1
            
        if not cred.enabled or not model.enabled:
            if diagnostics is not None:
                diagnostics.setdefault("disabled", 0)
                diagnostics["disabled"] += 1
            continue
            
        if await is_provider_failed(cred.id, model.model_id):
            if diagnostics is not None:
                diagnostics["failed"] += 1
            continue
            
        if needs_functions and not model.supports_functions:
            if diagnostics is not None:
                diagnostics["no_tools"] += 1
            continue
            
        quota = await get_quota_for_model(cred.id, model.model_id)
        if quota is None:
            quota = 999999999
        candidates_list.append(Candidate(
            credential_id=cred.id,
            provider=provider_name,
            model_id=model.model_id,
            cost=float(model.input_cost_per_1k) + float(model.output_cost_per_1k),
            quota=quota,
            input_cost_per_1k=float(model.input_cost_per_1k),
            output_cost_per_1k=float(model.output_cost_per_1k),
        ))

    if candidates_list:
        candidates_list.sort(key=lambda x: (x.cost, -x.quota))
        return candidates_list
    return None


async def _resolve_direct_model_id(
    session: AsyncSession,
    model_id: str,
    needs_functions: bool = False,
    diagnostics: dict = None,
) -> List["Candidate"]:
    """Look up a direct model ID across all enabled providers and credentials.

    Used as a passthrough when the requested model string is not a known tier alias.
    Handles both bare model IDs ('gemini-3.1-pro-preview') and provider-prefixed IDs
    ('fireworks/models/kimi-k2-thinking').
    """
    from shared.models import Provider

    candidates_list: List["Candidate"] = []

    # Try the model_id as-is, then strip everything up to and including the last provider-prefix
    # segment to handle 'fireworks/models/kimi-k2-thinking' → 'kimi-k2-thinking' etc.
    candidate_ids = [model_id]
    if "/" in model_id:
        # Also try the tail (last segment) in case of compound paths
        candidate_ids.append(model_id.rsplit("/", 1)[-1])

    stmt = (
        select(Credential, ProviderModel, Provider.name)
        .join(ProviderModel, Credential.provider_id == ProviderModel.provider_id)
        .join(Provider, Credential.provider_id == Provider.id)
        .where(Provider.enabled == True)
    )
    result = await session.execute(stmt)

    seen: set = set()
    for cred, model, provider_name in result:
        if model.model_id not in candidate_ids:
            continue

        if diagnostics is not None:
            diagnostics["total"] += 1

        if not cred.enabled or not model.enabled:
            if diagnostics is not None:
                diagnostics.setdefault("disabled", 0)
                diagnostics["disabled"] += 1
            continue

        if await is_provider_failed(cred.id, model.model_id):
            if diagnostics is not None:
                diagnostics["failed"] += 1
            continue

        if needs_functions and not model.supports_functions:
            if diagnostics is not None:
                diagnostics["no_tools"] += 1
            continue

        quota = await get_quota_for_model(cred.id, model.model_id)
        if quota is None:
            quota = 999999999

        key = (str(cred.id), model.model_id)
        if key not in seen:
            seen.add(key)
            candidates_list.append(Candidate(
                credential_id=cred.id,
                provider=provider_name,
                model_id=model.model_id,
                cost=float(model.input_cost_per_1k) + float(model.output_cost_per_1k),
                quota=quota,
                input_cost_per_1k=float(model.input_cost_per_1k),
                output_cost_per_1k=float(model.output_cost_per_1k),
            ))

    # Sort by cost ascending, quota descending (cheapest_available semantics)
    candidates_list.sort(key=lambda x: (x.cost, -x.quota))
    return candidates_list


async def get_ranked_candidates(session: AsyncSession, alias: str, request: ChatRequest, _is_fallback: bool = False, diagnostics: dict = None) -> List[Candidate]:
    """
    Returns an ordered list of all viable candidates for the given tier alias.
    The caller is responsible for trying them in order and falling back on error.
    Raises RuntimeError if no candidates are available at all.
    """
    logger.info("get_ranked_candidates: alias='%s' is_fallback=%s", alias, _is_fallback)
    if diagnostics is None:
        diagnostics = {"total": 0, "failed": 0, "no_tools": 0, "quota": 0, "disabled": 0}
    # Remember the original alias before any normalization (needed for direct model ID lookup)
    original_alias = alias
    # Strip a single leading-provider prefix that clients like LiteLLM add (e.g. "openai/gpt-4o")
    # but only when the result is *not* itself a slash-containing path, so we don't mangle
    # paths like 'fireworks/models/kimi-k2-thinking' into 'models/kimi-k2-thinking'.
    if "/" in alias:
        stripped = alias.split("/", 1)[1]
        # Only use the stripped version if it is a single-segment identifier (no further slashes)
        # OR if the stripped version matches a known tier alias
        known_tiers = {"lite", "base", "thinking", "auto"}
        if "/" not in stripped or stripped in known_tiers:
            alias = stripped
            logger.debug("Stripped provider prefix: '%s' → '%s'", original_alias, alias)

    needs_functions = bool(getattr(request, "model_extra", {}) and request.model_extra.get("tools"))

    # --- Handle 'auto' alias: task-aware selection with fallback chain ---
    if alias == "auto":
        all_candidates = []
        
        # 1. Try task-specific model matching (e.g., code tasks → coding models)
        task_candidates = await _try_task_specific_models(session, request, diagnostics)
        if task_candidates:
            all_candidates.extend(task_candidates)

        # 2. Fall back to tier-based routing with task-aware tier selection
        chosen = _auto_select_tier(request)
        fallback_order = {
            "thinking": ["thinking", "base", "lite"],
            "base": ["base", "lite", "thinking"],
            "lite": ["lite", "base", "thinking"],
        }
        
        seen_keys = {(str(c.credential_id), c.model_id) for c in all_candidates}
        
        for tier in fallback_order.get(chosen, ["lite"]):
            try:
                # To prevent infinite recursion, we lookup the tier directly
                tier_cands = await get_ranked_candidates(session, tier, request, _is_fallback=True, diagnostics=diagnostics)
                if tier_cands:
                    for c in tier_cands:
                        key = (str(c.credential_id), c.model_id)
                        if key not in seen_keys:
                            all_candidates.append(c)
                            seen_keys.add(key)
            except RuntimeError:
                pass
                
        if all_candidates:
            logger.info("Auto-tier resolved %d candidate(s)", len(all_candidates))
            return all_candidates
            
        diag_str = f"(Evaluated {diagnostics.get('total', 0)} candidates: {diagnostics.get('disabled', 0)} disabled in UI, {diagnostics.get('failed', 0)} failed health check, {diagnostics.get('quota', 0)} out of quota, {diagnostics.get('no_tools', 0)} missing tools support)"
        logger.warning("Auto-tier exhausted: %s", diag_str)
        raise RuntimeError(f"No valid routing candidates found for auto tier selection. {diag_str}")

    if alias in ["lite", "base", "thinking"]:
        if not _is_fallback:
            all_candidates = []
            fallback_order = {
                "thinking": ["thinking", "base", "lite"],
                "base": ["base", "lite", "thinking"],
                "lite": ["lite", "base", "thinking"],
            }
            for tier in fallback_order.get(alias, [alias]):
                try:
                    tier_cands = await get_ranked_candidates(session, tier, request, _is_fallback=True, diagnostics=diagnostics)
                    if tier_cands:
                        all_candidates.extend(tier_cands)
                except RuntimeError:
                    pass
            if all_candidates:
                return all_candidates
            diag_str = f"(Evaluated {diagnostics.get('total', 0)} candidates: {diagnostics.get('disabled', 0)} disabled in UI, {diagnostics.get('failed', 0)} failed health check, {diagnostics.get('quota', 0)} out of quota, {diagnostics.get('no_tools', 0)} missing tools support)"
            raise RuntimeError(f"No valid routing candidates found for explicit tier '{alias}'. {diag_str}")
        
        # If we are in fallback mode (_is_fallback=True), we DO NOT query the config's top-level fallback
        # again, but we DO process this specific tier's config/database resolution below.
        from shared.models import Provider
        stmt = (
            select(Credential, ProviderModel, Provider.name)
            .join(ProviderModel, Credential.provider_id == ProviderModel.provider_id)
            .join(Provider, Credential.provider_id == Provider.id)
            .where(
                ProviderModel.tier == alias,
                Provider.enabled == True
            )
        )
        result = await session.execute(stmt)
        candidates_list = []
        seen = set()  # (credential_id, model_id) dedup
        for cred, model, provider_name in result:
            if diagnostics is not None:
                diagnostics["total"] += 1
                
            if not cred.enabled or not model.enabled:
                if diagnostics is not None:
                    diagnostics.setdefault("disabled", 0)
                    diagnostics["disabled"] += 1
                continue
                
            if await is_provider_failed(cred.id, model.model_id):
                if diagnostics is not None:
                    diagnostics["failed"] += 1
                continue
                
            if needs_functions and not model.supports_functions:
                if diagnostics is not None:
                    diagnostics["no_tools"] += 1
                continue
                
            quota = await get_quota_for_model(cred.id, model.model_id)
            if quota is None:
                quota = 999999999
            
            candidates_list.append(Candidate(
                credential_id=cred.id,
                provider=provider_name,
                model_id=model.model_id,
                cost=float(model.input_cost_per_1k) + float(model.output_cost_per_1k),
                quota=quota,
                input_cost_per_1k=float(model.input_cost_per_1k),
                output_cost_per_1k=float(model.output_cost_per_1k),
            ))
            seen.add((str(cred.id), model.model_id))

        # Also merge candidates from routing.yaml for this tier
        config = get_routing_config()
        yaml_tier = config.get("tiers", {}).get(alias)
        if yaml_tier:
            yaml_models = yaml_tier.get("models", [])
            min_quota = yaml_tier.get("min_quota_remaining", 0)
            for m in yaml_models:
                prov = m.get("provider")
                mod = m.get("model")
                if not prov or not mod:
                    continue
                yaml_cands = await get_candidate_details(session, prov, mod, needs_functions=needs_functions, diagnostics=diagnostics)
                for c in yaml_cands:
                    if (str(c.credential_id), c.model_id) not in seen:
                        if c.quota >= min_quota:
                            candidates_list.append(c)
                            seen.add((str(c.credential_id), c.model_id))
                        else:
                            if diagnostics is not None:
                                diagnostics["quota"] += 1
            
        if candidates_list:
            # Cheapest available for virtual models
            candidates_list.sort(key=lambda x: (x.cost, -x.quota))
            return candidates_list

    config = get_routing_config()
    tier_config = config.get("tiers", {}).get(alias)

    if not tier_config:
        diag_str = f"(Evaluated {diagnostics.get('total', 0)} candidates: {diagnostics.get('disabled', 0)} disabled in UI, {diagnostics.get('failed', 0)} failed health check, {diagnostics.get('quota', 0)} out of quota, {diagnostics.get('no_tools', 0)} missing tools support)"
        if alias in ["lite", "base", "thinking"] or _is_fallback:
            raise RuntimeError(f"No valid routing candidates found for core tier '{alias}'. {diag_str}")

        # ── Direct model ID passthrough ──────────────────────────────────────
        # The client sent a concrete model name (e.g. 'gemini-1.5-pro' or
        # 'fireworks/models/kimi-k2-thinking') rather than a tier alias.
        # Try to locate matching candidates across all enabled providers.
        needs_functions = bool(getattr(request, "model_extra", {}) and request.model_extra.get("tools"))
        direct_candidates = await _resolve_direct_model_id(
            session, original_alias, needs_functions=needs_functions, diagnostics=diagnostics
        )
        if direct_candidates:
            logger.info("Direct model-ID passthrough: '%s' → %d candidate(s)", original_alias, len(direct_candidates))
            return direct_candidates

        # Nothing matched — surface a clear error
        raise RuntimeError(
            f"Model '{original_alias}' not found: it is neither a tier alias "
            f"(lite/base/thinking/auto/custom) nor a model ID registered in any enabled provider. "
            f"Add it to a provider in Model Management or configure a tier alias in routing.yaml. "
            f"{diag_str}"
        )

    models = tier_config.get("models", [])
    strategy = tier_config.get("strategy", "cheapest_available")
    min_quota = tier_config.get("min_quota_remaining", 0)

    # Resolve candidates from YAML config
    candidates: List[Candidate] = []
    seen = set()
    for m in models:
        provider = m.get("provider")
        model = m.get("model")
        if not provider or not model:
            continue
        valid_candidates = await get_candidate_details(session, provider, model, needs_functions=needs_functions, diagnostics=diagnostics)
        for c in valid_candidates:
            if c.quota >= min_quota:
                candidates.append(c)
                seen.add((str(c.credential_id), c.model_id))
            else:
                if diagnostics is not None:
                    diagnostics["quota"] += 1

    # If the tier is known in the DB (lite, base, thinking) but YAML models were empty or all failed,
    # fallback to querying the DB so we don't return an empty array.
    if alias in ["lite", "base", "thinking"]:
        from shared.models import Provider
        stmt = (
            select(Credential, ProviderModel, Provider.name)
            .join(ProviderModel, Credential.provider_id == ProviderModel.provider_id)
            .join(Provider, Credential.provider_id == Provider.id)
            .where(
                ProviderModel.tier == alias,
                Provider.enabled == True
            )
        )
        result = await session.execute(stmt)
        for cred, model, provider_name in result:
            key = (str(cred.id), model.model_id)
            if key in seen:
                continue
            
            if diagnostics is not None:
                diagnostics["total"] += 1
                
            if not cred.enabled or not model.enabled:
                if diagnostics is not None:
                    diagnostics.setdefault("disabled", 0)
                    diagnostics["disabled"] += 1
                continue
                
            if await is_provider_failed(cred.id, model.model_id):
                if diagnostics is not None:
                    diagnostics["failed"] += 1
                continue
                
            if needs_functions and not model.supports_functions:
                if diagnostics is not None:
                    diagnostics["no_tools"] += 1
                continue
                
            quota = await get_quota_for_model(cred.id, model.model_id)
            if quota is None:
                quota = 999999999
                
            if quota >= min_quota:
                candidates.append(Candidate(
                    credential_id=cred.id,
                    provider=provider_name,
                    model_id=model.model_id,
                    cost=float(model.input_cost_per_1k) + float(model.output_cost_per_1k),
                    quota=quota,
                    input_cost_per_1k=float(model.input_cost_per_1k),
                    output_cost_per_1k=float(model.output_cost_per_1k),
                ))
            else:
                if diagnostics is not None:
                    diagnostics["quota"] += 1

    if not candidates:
        diag_str = f"(Evaluated {diagnostics.get('total', 0)} candidates: {diagnostics.get('disabled', 0)} disabled in UI, {diagnostics.get('failed', 0)} failed health check, {diagnostics.get('quota', 0)} out of quota, {diagnostics.get('no_tools', 0)} missing tools support)"
        raise RuntimeError(f"No valid routing candidates found for tier '{alias}'. {diag_str}")

    # Sort / order based on strategy
    if strategy == "cheapest_available":
        # Sort by cost ascending, then quota descending
        candidates.sort(key=lambda x: (x.cost, -x.quota))
    elif strategy == "highest_quota":
        # Sort by quota descending, then cost ascending
        candidates.sort(key=lambda x: (-x.quota, x.cost))
    elif strategy == "brain_optimized":
        # Sort using brain metrics: health, quota, cost
        from brain.tester import get_cached_health
        
        async def score_candidate(c: Candidate) -> float:
            cached = await get_cached_health(c.credential_id, c.model_id)
            if cached:
                health_ok = cached.get("ok", False)
                latency_ms = int(cached.get("latency_ms", 10000))
            else:
                health_ok = False
                latency_ms = 10000
                
            health_norm = 1.0 if health_ok else 0.0
            quota_norm = min(1.0, max(c.quota, 0) / 1000000.0) if c.quota >= 0 else 0.5
            latency_norm = max(0.0, 1.0 - min(latency_ms, 10000) / 10000.0)
            
            # Use cost directly
            # Higher score is better
            return (0.4 * health_norm) + (0.3 * quota_norm) + (0.2 * latency_norm) - (0.1 * c.cost)
            
        scored = []
        for c in candidates:
            score = await score_candidate(c)
            scored.append((score, c))
            
        scored.sort(key=lambda x: -x[0])  # Sort descending by score
        candidates = [sc[1] for sc in scored]

    logger.debug(
        "Ranked %d candidates for '%s' (strategy=%s): diagnostics=%s",
        len(candidates), alias, strategy if 'strategy' in dir() else 'default', diagnostics,
    )
    return candidates


async def select_model(session: AsyncSession, alias: str, request: ChatRequest) -> Tuple[UUID, str, str]:
    """
    Selects the top-ranked model for the given tier alias.
    Returns: (credential_id, provider_name, model_id)

    For fallback support, use get_ranked_candidates() directly and iterate.
    """
    candidates = await get_ranked_candidates(session, alias, request)
    best = candidates[0]
    logger.info("select_model: alias='%s' → provider=%s model=%s", alias, best.provider, best.model_id)
    return best.credential_id, best.provider, best.model_id
