"""
wizard.py — Setup Wizard API routes.

Provides endpoints to drive the guided provider-onboarding wizard from
either the GUI or the CLI.  The wizard orchestrates existing CRUD
operations (providers, credentials, models, routing config, brain) in a
single transactional request.

Endpoints
─────────
  GET  /admin/wizard/providers/available
       Returns all seed-catalog providers merged with their current DB
       status (has_credentials, enabled, credentials_count).

  GET  /admin/wizard/models/{provider_name}
       Returns the static model catalog for a provider (no live API call).

  POST /admin/wizard/onboard
       Accepts a full wizard payload and persists providers, credentials,
       models, routing YAML, and brain assignments in a single request.
"""

from __future__ import annotations

import uuid
import datetime
import logging
import yaml as _yaml
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from shared.database import get_db_session
from shared.models import (
    BrainConfig,
    Credential,
    GatewayKey,
    Provider,
    ProviderModel,
    RoutingConfig,
)
from shared.security import encrypt_secret

from api_gateway.auth import require_admin_key
from api_gateway.routes.model_catalog import ModelEntry, get_catalog
from api_gateway.routes.seeds import _PROVIDER_SEED

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/wizard", tags=["Wizard"])


# ─────────────────────────────────────────────────────────────
# Pydantic request / response models
# ─────────────────────────────────────────────────────────────

class AvailableProviderResponse(BaseModel):
    name: str
    display_name: str
    auth_type: str
    id: Optional[str] = None
    enabled: bool = True
    has_credentials: bool = False
    credentials_count: int = 0
    has_catalog: bool = False


class WizardCredential(BaseModel):
    label: str
    secret_key: str
    auth_type: str = "api_key"


class WizardModel(BaseModel):
    model_id: str
    display_name: str
    tier: str = ""
    context_window: int = 128_000
    input_cost_per_1k: float = 0.0
    output_cost_per_1k: float = 0.0
    supports_streaming: bool = True
    supports_functions: bool = True
    enabled: bool = True


class WizardRoutingTierModel(BaseModel):
    provider: str
    model: str


class WizardRoutingTier(BaseModel):
    strategy: str = "cheapest_available"
    fallback_on: List[str] = Field(default_factory=lambda: [429, 503, "timeout"])
    models: List[WizardRoutingTierModel] = Field(default_factory=list)


class WizardBrainEntry(BaseModel):
    provider_name: str
    credential_label: str
    model_id: str
    priority: int = 100


class WizardProviderPayload(BaseModel):
    provider_name: str
    credentials: List[WizardCredential] = Field(default_factory=list)
    models: List[WizardModel] = Field(default_factory=list)


class WizardOnboardRequest(BaseModel):
    providers: List[WizardProviderPayload] = Field(default_factory=list)
    routing_tiers: Dict[str, WizardRoutingTier] = Field(default_factory=dict)
    brain_entries: List[WizardBrainEntry] = Field(default_factory=list)


class WizardOnboardResponse(BaseModel):
    ok: bool
    summary: Dict[str, Any]


# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

def _seed_map() -> Dict[str, Dict[str, Any]]:
    """Return seed catalog keyed by provider name."""
    return {s["name"]: s for s in _PROVIDER_SEED}


async def _get_or_create_provider(
    session: AsyncSession,
    name: str,
    seed: Dict[str, Any],
) -> Provider:
    """Find an existing provider by name or create from seed data."""
    stmt = select(Provider).where(Provider.name == name)
    result = await session.execute(stmt)
    provider = result.scalar_one_or_none()
    if not provider:
        provider = Provider(
            id=uuid.uuid4(),
            name=name,
            display_name=seed.get("display_name", name),
            auth_type=seed.get("auth_type", "api_key"),
            base_url=seed.get("base_url"),
            oauth_meta=seed.get("oauth_meta"),
            enabled=True,
        )
        session.add(provider)
        await session.flush()  # get the ID without committing
    return provider


# ─────────────────────────────────────────────────────────────
# Endpoints
# ─────────────────────────────────────────────────────────────

@router.get("/providers/available", response_model=List[AvailableProviderResponse])
async def get_available_providers(
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Return all seed-catalog providers merged with their current DB state.

    Each entry carries:
    - name, display_name, auth_type (from seed)
    - id, enabled (from DB, if already created)
    - has_credentials, credentials_count (from DB)
    - has_catalog (True if static model catalog exists)
    """
    from api_gateway.routes.model_catalog import all_providers_with_catalog

    # Fetch existing DB providers
    stmt = select(Provider).order_by(Provider.name)
    result = await session.execute(stmt)
    existing: Dict[str, Provider] = {p.name: p for p in result.scalars().all()}

    # Fetch credential counts per provider
    cred_stmt = select(Credential.provider_id)
    cred_result = await session.execute(cred_stmt)
    cred_counts: Dict[str, int] = {}
    for (pid,) in cred_result:
        cred_counts[str(pid)] = cred_counts.get(str(pid), 0) + 1

    catalog_providers = set(all_providers_with_catalog())
    seed = _seed_map()
    items: list[AvailableProviderResponse] = []

    for s in _PROVIDER_SEED:
        pname = s["name"]
        db_p = existing.get(pname)
        cred_count = cred_counts.get(str(db_p.id), 0) if db_p else 0
        items.append(
            AvailableProviderResponse(
                name=pname,
                display_name=s["display_name"],
                auth_type=s["auth_type"],
                id=str(db_p.id) if db_p else None,
                enabled=db_p.enabled if db_p else True,
                has_credentials=cred_count > 0,
                credentials_count=cred_count,
                has_catalog=pname in catalog_providers,
            )
        )

    return items


@router.get("/models/{provider_name}")
async def get_wizard_models(
    provider_name: str,
    key: GatewayKey = Depends(require_admin_key),
):
    """Return the static model catalog for a provider."""
    catalog: list[ModelEntry] = get_catalog(provider_name)
    if not catalog:
        # Return empty list with a note rather than 404 so the wizard can
        # still render a free-text entry fallback.
        return {"provider": provider_name, "has_catalog": False, "models": []}
    return {"provider": provider_name, "has_catalog": True, "models": catalog}


@router.post("/onboard", response_model=WizardOnboardResponse)
async def wizard_onboard(
    payload: WizardOnboardRequest,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session),
):
    """
    Execute a full wizard onboarding:

    1. Create/ensure providers exist.
    2. Create credentials for each provider.
    3. Create/enable models for each provider.
    4. Save routing YAML if tiers are provided.
    5. Create brain assignments.

    All steps run in a single DB transaction; if any step fails the whole
    request is rolled back.
    """
    seed = _seed_map()
    summary: Dict[str, Any] = {
        "providers": [],
        "credentials": [],
        "models": [],
        "routing": None,
        "brain": [],
    }

    try:
        # ── Step 1 & 2 & 3 — providers, credentials, models ──────────────────
        logger.info("Wizard onboard started: %d provider(s), %d routing tier(s), %d brain entry(ies)",
                    len(payload.providers), len(payload.routing_tiers), len(payload.brain_entries))

        # Track provider_name → Provider ORM object (for brain step)
        provider_map: Dict[str, Provider] = {}
        # Track (provider_name, credential_label) → Credential ORM object
        cred_map: Dict[tuple, Credential] = {}

        for p_payload in payload.providers:
            pname = p_payload.provider_name
            p_seed = seed.get(pname, {"name": pname, "display_name": pname, "auth_type": "api_key"})
            provider = await _get_or_create_provider(session, pname, p_seed)
            provider_map[pname] = provider
            summary["providers"].append({"name": pname, "id": str(provider.id)})

            # Credentials
            for cred_data in p_payload.credentials:
                secret_enc, iv = b"", None
                if cred_data.secret_key:
                    try:
                        secret_enc, iv = encrypt_secret(cred_data.secret_key)
                    except Exception:
                        secret_enc = b"ENCRYPTION_FAILED"

                # Check if a credential with the same label already exists
                existing_cred_stmt = select(Credential).where(
                    Credential.provider_id == provider.id,
                    Credential.label == cred_data.label,
                )
                existing_cred_result = await session.execute(existing_cred_stmt)
                cred = existing_cred_result.scalar_one_or_none()

                if cred is None:
                    cred = Credential(
                        id=uuid.uuid4(),
                        provider_id=provider.id,
                        label=cred_data.label,
                        auth_type=cred_data.auth_type,
                        secret_enc=secret_enc,
                        iv=iv,
                        enabled=True,
                    )
                    session.add(cred)
                    await session.flush()

                cred_map[(pname, cred_data.label)] = cred
                summary["credentials"].append({
                    "provider": pname,
                    "label": cred_data.label,
                    "id": str(cred.id),
                })

            # Models
            for m_data in p_payload.models:
                # Skip if already exists
                existing_m_stmt = select(ProviderModel).where(
                    ProviderModel.provider_id == provider.id,
                    ProviderModel.model_id == m_data.model_id,
                )
                existing_m = await session.execute(existing_m_stmt)
                model_obj = existing_m.scalar_one_or_none()

                if model_obj is None:
                    model_obj = ProviderModel(
                        id=uuid.uuid4(),
                        provider_id=provider.id,
                        model_id=m_data.model_id,
                        display_name=m_data.display_name or m_data.model_id,
                        context_window=m_data.context_window,
                        input_cost_per_1k=m_data.input_cost_per_1k,
                        output_cost_per_1k=m_data.output_cost_per_1k,
                        tier=m_data.tier or "",
                        supports_streaming=m_data.supports_streaming,
                        supports_functions=m_data.supports_functions,
                        enabled=m_data.enabled,
                    )
                    session.add(model_obj)
                else:
                    # Re-enable if it was disabled
                    model_obj.enabled = m_data.enabled
                    model_obj.tier = m_data.tier or model_obj.tier

                summary["models"].append({
                    "provider": pname,
                    "model_id": m_data.model_id,
                })

        # ── Step 4 — routing YAML ─────────────────────────────────────────────
        if payload.routing_tiers:
            tiers_dict: Dict[str, Any] = {}
            for tier_name, tier_cfg in payload.routing_tiers.items():
                tiers_dict[tier_name] = {
                    "strategy": tier_cfg.strategy,
                    "fallback_on": tier_cfg.fallback_on,
                    "models": [
                        {"provider": m.provider, "model": m.model}
                        for m in tier_cfg.models
                    ],
                }
            yaml_content = _yaml.dump({"tiers": tiers_dict}, default_flow_style=False, sort_keys=False)

            # Upsert the single RoutingConfig record
            rc_stmt = select(RoutingConfig).limit(1)
            rc_result = await session.execute(rc_stmt)
            cfg = rc_result.scalar_one_or_none()
            if cfg:
                cfg.yaml_content = yaml_content
                cfg.updated_at = datetime.datetime.utcnow()
            else:
                cfg = RoutingConfig(
                    id=uuid.uuid4(),
                    yaml_content=yaml_content,
                    updated_at=datetime.datetime.utcnow(),
                )
                session.add(cfg)

            summary["routing"] = {"tiers": list(tiers_dict.keys())}

        # ── Step 5 — brain assignments ────────────────────────────────────────
        for brain_entry in payload.brain_entries:
            bname = brain_entry.provider_name
            provider = provider_map.get(bname)
            if not provider:
                # Provider may already be in DB but not in this wizard payload
                stmt = select(Provider).where(Provider.name == bname)
                result = await session.execute(stmt)
                provider = result.scalar_one_or_none()
            if not provider:
                raise HTTPException(
                    status_code=400,
                    detail=f"Brain: provider '{bname}' not found — onboard it first.",
                )

            cred = cred_map.get((bname, brain_entry.credential_label))
            if not cred:
                # Credential may already exist in DB
                stmt = select(Credential).where(
                    Credential.provider_id == provider.id,
                    Credential.label == brain_entry.credential_label,
                )
                result = await session.execute(stmt)
                cred = result.scalar_one_or_none()
            if not cred:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"Brain: credential '{brain_entry.credential_label}' "
                        f"for provider '{bname}' not found."
                    ),
                )

            brain_obj = BrainConfig(
                id=uuid.uuid4(),
                provider_id=provider.id,
                credential_id=cred.id,
                model_id=brain_entry.model_id,
                priority=brain_entry.priority,
                enabled=True,
            )
            session.add(brain_obj)
            summary["brain"].append({
                "provider": bname,
                "credential": brain_entry.credential_label,
                "model_id": brain_entry.model_id,
                "priority": brain_entry.priority,
            })

        await session.commit()
        
        # Trigger sync to router/litellm
        from router.quota import trigger_provider_sync
        from fastapi import BackgroundTasks
        # We can't inject BackgroundTasks easily here since we didn't add it to the route params.
        # Let's import it and either call it directly or just add BackgroundTasks to route and use it.
        # It's an async function so we can await it or use asyncio.create_task

        import asyncio
        asyncio.create_task(trigger_provider_sync())

        return WizardOnboardResponse(ok=True, summary=summary)

    except HTTPException:
        await session.rollback()
        raise
    except Exception as exc:
        await session.rollback()
        logger.error("Wizard onboard failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Wizard onboard failed: {str(exc)}") from exc
