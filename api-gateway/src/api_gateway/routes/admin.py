from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
from pydantic import BaseModel
import datetime
import logging
import uuid
import json
import jwt
import yaml
import os
from typing import List, Optional, Dict, Any
from uuid import UUID

from shared.database import get_db_session
from shared.models import Provider, Credential, ProviderModel, GatewayKey, RequestLog, RoutingConfig
from shared.security import unwrap_secret
from shared.schemas import (
    ProviderCreate, ProviderUpdate, ProviderResponse,
    CredentialCreate, CredentialUpdate, CredentialResponse,
    ModelCreate, ModelUpdate, ModelResponse,
    GatewayKeyCreate, GatewayKeyUpdate, GatewayKeyResponse,
    RoutingConfigUpdate, LogStatsResponse, UsageStatsResponse, LogResponse,
    ProviderUsageResponse
)
from shared.security import encrypt_secret
from router.quota import trigger_provider_sync

from api_gateway.auth import require_admin_key, JWT_SECRET, JWT_ALGORITHM

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["Admin"])

class LoginRequest(BaseModel):
    password: str

@router.post("/login")
async def admin_login(req: LoginRequest, response: Response, session: AsyncSession = Depends(get_db_session)):
    import os
    admin_password = unwrap_secret(os.environ.get("MASTER_PASSWORD") or os.environ.get("ADMIN_PASSWORD", "admin"))
    if req.password != admin_password:
        logger.warning("Admin login failed: invalid password")
        raise HTTPException(status_code=401, detail="Invalid password")

    # Look up an admin-scoped gateway key to return its raw token
    stmt = select(GatewayKey).where(GatewayKey.enabled == True)
    result = await session.execute(stmt)
    all_keys = result.scalars().all()
    admin_key = next((k for k in all_keys if "admin" in (k.scopes or [])), None)

    if not admin_key or not admin_key.raw_token:
        logger.error("Admin login: no admin gateway key found in database")
        raise HTTPException(status_code=500, detail="No admin key configured. Please create an admin key first.")

    token = admin_key.raw_token
    response.set_cookie("gateway_jwt", "", httponly=True, max_age=0, samesite="lax")  # clear any stale JWT cookie
    logger.info("Admin login successful (returned admin gateway key)")
    return {"status": "success", "token": token}

@router.post("/logout")
async def admin_logout(response: Response):
    response.delete_cookie("gateway_jwt")
    return {"status": "success"}

@router.get("/providers", response_model=List[ProviderResponse])
async def list_providers(
    key: GatewayKey = Depends(require_admin_key), 
    session: AsyncSession = Depends(get_db_session)
):
    stmt = select(Provider).order_by(Provider.name)
    result = await session.execute(stmt)
    return result.scalars().all()

@router.post("/providers", response_model=ProviderResponse)
async def create_provider(
    provider: ProviderCreate,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    if provider.auth_type not in ["api_key", "oauth2"]:
        raise HTTPException(status_code=400, detail="Invalid auth_type")
        
    db_obj = Provider(
        id=uuid.uuid4(),
        **provider.model_dump()
    )
    session.add(db_obj)
    await session.commit()
    await session.refresh(db_obj)
    logger.info("Created provider: name=%s id=%s", provider.name, db_obj.id)
    return db_obj

@router.patch("/providers/{id}", response_model=ProviderResponse)
async def update_provider(
    id: uuid.UUID,
    updates: ProviderUpdate,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = select(Provider).where(Provider.id == id)
    result = await session.execute(stmt)
    db_obj = result.scalar_one_or_none()
    
    if not db_obj:
        raise HTTPException(status_code=404, detail="Provider not found")
        
    update_data = updates.model_dump(exclude_unset=True)
    for k, v in update_data.items():
        setattr(db_obj, k, v)
        
    await session.commit()
    await session.refresh(db_obj)
    return db_obj

@router.delete("/providers/{id}")
async def delete_provider(
    id: uuid.UUID,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = select(Provider).where(Provider.id == id)
    result = await session.execute(stmt)
    provider = result.scalar_one_or_none()
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
        
    cred_stmt = select(Credential).where(Credential.provider_id == id, Credential.enabled == True)
    cred_result = await session.execute(cred_stmt)
    active_creds = cred_result.scalars().all()
    if active_creds:
        raise HTTPException(status_code=400, detail="Cannot delete provider with active credentials. Please disable or delete them first.")
        
    delete_stmt = delete(Provider).where(Provider.id == id)
    await session.execute(delete_stmt)
    await session.commit()
    return {"status": "success"}

# Add back _PROVIDER_SEED array from old main.py in actual implementation
from .seeds import _PROVIDER_SEED

class SeedRequest(BaseModel):
    providers: Optional[List[str]] = None

@router.get("/providers/seeds")
async def get_provider_seeds(
    key: GatewayKey = Depends(require_admin_key)
):
    return _PROVIDER_SEED

@router.post("/providers/seed")
async def seed_providers(
    req: SeedRequest = None,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = select(Provider.name)
    result = await session.execute(stmt)
    existing = set(result.scalars().all())
    
    selected_names = set(req.providers) if req and req.providers else None

    inserted = []
    skipped = 0
    for seed in _PROVIDER_SEED:
        if selected_names and seed["name"] not in selected_names:
            continue
        if seed["name"] not in existing:
            p = Provider(id=uuid.uuid4(), **seed)
            session.add(p)
            inserted.append(seed["name"])
        else:
            skipped += 1
            
    await session.commit()
    return {"inserted": inserted, "skipped": skipped}


@router.get("/credentials", response_model=List[CredentialResponse])
async def list_credentials(
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = select(Credential).order_by(Credential.label)
    result = await session.execute(stmt)
    return result.scalars().all()

@router.post("/credentials", response_model=CredentialResponse)
async def create_credential(
    cred: CredentialCreate,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    # Verify provider exists
    p_stmt = select(Provider).where(Provider.id == cred.provider_id)
    p_result = await session.execute(p_stmt)
    if not p_result.scalar_one_or_none():
        raise HTTPException(status_code=400, detail="Invalid provider_id")

    iv = None
    if cred.secret_key:
        try:
            secret_enc, iv = encrypt_secret(cred.secret_key)
        except Exception as e:
            secret_enc = b"ENCRYPTION_FAILED"
    else:
        secret_enc = b""
        
    db_obj = Credential(
        id=uuid.uuid4(),
        provider_id=cred.provider_id,
        label=cred.label,
        auth_type=cred.auth_type,
        secret_enc=secret_enc,
        iv=iv,
        enabled=cred.enabled
    )
    session.add(db_obj)
    await session.commit()
    await session.refresh(db_obj)
    logger.info("Created credential: label=%s provider=%s id=%s", cred.label, cred.provider_id, db_obj.id)
    return db_obj

@router.patch("/credentials/{id}", response_model=CredentialResponse)
async def update_credential(
    id: uuid.UUID,
    updates: CredentialUpdate,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = select(Credential).where(Credential.id == id)
    result = await session.execute(stmt)
    db_obj = result.scalar_one_or_none()
    
    if not db_obj:
        raise HTTPException(status_code=404, detail="Credential not found")
        
    update_data = updates.model_dump(exclude_unset=True)
    for k, v in update_data.items():
        setattr(db_obj, k, v)
        
    await session.commit()
    await session.refresh(db_obj)
    return db_obj

@router.delete("/credentials/{id}")
async def delete_credential(
    id: uuid.UUID,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = delete(Credential).where(Credential.id == id)
    result = await session.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Credential not found")
    await session.commit()
    return {"status": "success"}

@router.get("/credentials/{id}/verify")
async def verify_credential(
    id: uuid.UUID,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    """Check whether the saved credential can successfully reach the provider API."""
    from sqlalchemy.orm import selectinload
    from shared.security import decrypt_secret
    from brain.health import check_provider_health

    stmt = (
        select(Credential)
        .options(selectinload(Credential.provider))
        .where(Credential.id == id)
    )
    result = await session.execute(stmt)
    cred = result.scalar_one_or_none()

    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    try:
        api_key = decrypt_secret(cred.secret_enc, cred.iv)
        provider_name = cred.provider.name if cred.provider else "unknown"
        provider_display = getattr(cred.provider, "display_name", None) or provider_name
        base_url = cred.provider.base_url if cred.provider else None
        health = await check_provider_health(
            provider_name=provider_name,
            api_key=api_key,
            base_url=base_url,
        )
        if health.ok:
            message = f"Connected to {provider_display} – {health.latency_ms}ms"
        else:
            message = f"Cannot reach {provider_display}: {health.message}"
        return {
            "status": "success" if health.ok else "error",
            "message": message,
            "latency_ms": health.latency_ms,
            "status_code": health.status_code,
        }
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Verification failed: {exc}",
            "latency_ms": None,
            "status_code": None,
        }


@router.get("/credentials/{id}/quota")
async def get_credential_quota(
    id: uuid.UUID,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    """Return the cached quota information for a credential (from Redis)."""
    from router.quota import get_quota_for_model

    stmt = select(Credential).where(Credential.id == id)
    result = await session.execute(stmt)
    cred = result.scalar_one_or_none()

    if not cred:
        raise HTTPException(status_code=404, detail="Credential not found")

    # Try to look up quota from Redis (if available)
    try:
        quota = await get_quota_for_model(cred.id, "*")
        if quota is not None:
            return {
                "tokens_remaining": quota,
                "requests_remaining": None,  # not tracked separately yet
            }
    except Exception:
        pass

    return {
        "tokens_remaining": None,
        "requests_remaining": None,
        "message": "No quota data yet. Quota is polled automatically in the background.",
    }


@router.post("/providers/{id}/sync-models")
async def sync_provider_models(
    id: uuid.UUID,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    """Sync models from the provider's live API and insert any new ones into the DB.

    - Loads the first enabled credential for the provider
    - Calls adapter.list_models() which hits the provider's real /v1/models endpoint
    - Inserts any new models (disabled by default, tier = unassigned)
    - Falls back to the built-in catalog if the live API is unavailable
    """
    from sqlalchemy.orm import selectinload
    from shared.security import decrypt_secret
    from router.adapters import get_adapter
    from api_gateway.routes.model_catalog import get_catalog

    # Load provider with credentials
    stmt = (
        select(Provider)
        .options(selectinload(Provider.credentials))
        .where(Provider.id == id)
    )
    result = await session.execute(stmt)
    provider = result.scalar_one_or_none()

    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    # Find the first enabled credential
    cred = next((c for c in provider.credentials if c.enabled), None)

    # ── Fetch existing ProviderModel rows so we can update them ──────────────
    existing_stmt_full = select(ProviderModel).where(ProviderModel.provider_id == id)
    existing_res_full = await session.execute(existing_stmt_full)
    existing_models = {pm.model_id: pm for pm in existing_res_full.scalars().all()}
    before = len(existing_models)

    source = "api"
    model_infos = []

    # ── Live API sync (if we have a credential) ─────────────────────────────
    if cred:
        try:
            adapter = get_adapter(provider.name)
            model_infos = await adapter.list_models(cred)
        except Exception as exc:
            model_infos = []
            source = f"catalog (live API failed: {exc})"

    # ── Always merge static catalog models ──────────────────────────────────
    # The live API may only expose a subset of a provider's models (e.g., DeepSeek
    # only returns 2 of its models via /v1/models). We always merge the catalog so
    # every known model is available, with live API results taking priority.
    from router.adapters.base import ModelInfo
    catalog = get_catalog(provider.name)

    if not model_infos and not catalog:
        # No live models AND no catalog — nothing to do
        return {
            "status": "no_models",
            "provider": provider.name,
            "total": before,
            "inserted": 0,
            "updated": 0,
            "source": source,
            "message": (
                f"No models found for {provider.display_name or provider.name}. "
                "Add a credential first, or add models manually."
            ),
        }

    # Build a set of model IDs already captured from the live API
    live_model_ids = {info.model_id for info in model_infos}

    # Merge catalog: add any catalog model not already covered by live API
    for m in catalog:
        if m.model_id not in live_model_ids:
            model_infos.append(ModelInfo(
                model_id=m.model_id,
                display_name=m.display_name,
                context_window=m.context_window,
                input_cost_per_1k=m.input_cost_per_1k,
                output_cost_per_1k=m.output_cost_per_1k,
                supports_streaming=m.supports_streaming,
                supports_functions=m.supports_functions,
            ))

    if not model_infos:
        source = "catalog"
    elif catalog and live_model_ids:
        source = "api+catalog"


    # ── Insert new models / update existing ones ─────────────────────────────
    catalog_entries = get_catalog(provider.name)
    catalog_tiers = {m.model_id: m.tier for m in catalog_entries}

    inserted = 0
    updated = 0
    for info in model_infos:
        if not info.model_id:
            continue

        existing_pm = existing_models.get(info.model_id)
        if existing_pm:
            # Update pricing/metadata so costs don't stay at 0.0 from old syncs
            changed = False
            if info.input_cost_per_1k and float(existing_pm.input_cost_per_1k) != info.input_cost_per_1k:
                existing_pm.input_cost_per_1k = info.input_cost_per_1k
                changed = True
            if info.output_cost_per_1k and float(existing_pm.output_cost_per_1k) != info.output_cost_per_1k:
                existing_pm.output_cost_per_1k = info.output_cost_per_1k
                changed = True
            if info.context_window and existing_pm.context_window != info.context_window:
                existing_pm.context_window = info.context_window
                changed = True
            if info.display_name and existing_pm.display_name != info.display_name:
                existing_pm.display_name = info.display_name
                changed = True
            # Set tier from catalog if currently unset
            catalog_tier = catalog_tiers.get(info.model_id, "")
            if catalog_tier and not existing_pm.tier:
                existing_pm.tier = catalog_tier
                changed = True
            if changed:
                updated += 1
        else:
            # New model — insert (disabled until admin enables it)
            model_db = ProviderModel(
                provider_id=id,
                model_id=info.model_id,
                display_name=info.display_name,
                context_window=info.context_window,
                input_cost_per_1k=info.input_cost_per_1k,
                output_cost_per_1k=info.output_cost_per_1k,
                tier=catalog_tiers.get(info.model_id, ""),
                supports_streaming=info.supports_streaming,
                supports_functions=info.supports_functions,
                enabled=False,
            )
            session.add(model_db)
            inserted += 1

    await session.commit()
    logger.info(
        "Model sync for provider=%s: %d inserted, %d updated, %d total (source=%s)",
        provider.name, inserted, updated, before + inserted, source,
    )

    return {
        "status": "ok",
        "provider": provider.name,
        "total": before + inserted,
        "inserted": inserted,
        "updated": updated,
        "source": source,
        "message": (
            f"Synced {provider.display_name or provider.name} from {source}: "
            f"{inserted} new model(s) added, {updated} existing model(s) updated "
            f"({before + inserted} total)."
        ),
    }

# Models list moved to /v1/models router

@router.patch("/models/{id}", response_model=ModelResponse)
async def update_model(
    id: uuid.UUID,
    updates: ModelUpdate,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = select(ProviderModel).where(ProviderModel.id == id)
    result = await session.execute(stmt)
    db_obj = result.scalar_one_or_none()
    
    if not db_obj:
        raise HTTPException(status_code=404, detail="Model not found")
        
    update_data = updates.model_dump(exclude_unset=True)
    
    if "tier" in update_data and "enabled" not in update_data:
        update_data["enabled"] = bool(update_data["tier"])
        
    for k, v in update_data.items():
        setattr(db_obj, k, v)
        
    await session.commit()
    await session.refresh(db_obj)
    return db_obj

@router.delete("/models/{id}")
async def delete_model(
    id: uuid.UUID,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = delete(ProviderModel).where(ProviderModel.id == id)
    result = await session.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Model not found")
    await session.commit()
    return {"status": "success"}

@router.get("/keys", response_model=List[GatewayKeyResponse])
async def list_keys(
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = select(GatewayKey).order_by(GatewayKey.label)
    result = await session.execute(stmt)
    return result.scalars().all()

@router.post("/keys")
async def create_key(
    req: GatewayKeyCreate,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    import secrets
    import hashlib
    raw_token = f"sk-{secrets.token_urlsafe(32)}"
    key_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    
    db_obj = GatewayKey(
        id=uuid.uuid4(),
        label=req.label,
        key_hash=key_hash,
        raw_token=raw_token,
        scopes=req.scopes,
        enabled=True
    )
    session.add(db_obj)
    await session.commit()
    await session.refresh(db_obj)
    logger.info("Created API key: label=%s id=%s scopes=%s", req.label, db_obj.id, req.scopes)
    
    return {
        "id": db_obj.id,
        "label": db_obj.label,
        "token": raw_token,
        "scopes": db_obj.scopes,
        "enabled": db_obj.enabled
    }

@router.patch("/keys/{id}", response_model=GatewayKeyResponse)
async def update_key(
    id: uuid.UUID,
    updates: GatewayKeyUpdate,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = select(GatewayKey).where(GatewayKey.id == id)
    result = await session.execute(stmt)
    db_obj = result.scalar_one_or_none()
    
    if not db_obj:
        raise HTTPException(status_code=404, detail="Key not found")
        
    if updates.label is not None:
        if not updates.label.strip():
            raise HTTPException(status_code=422, detail="Label cannot be empty")
        db_obj.label = updates.label.strip()
        
    await session.commit()
    await session.refresh(db_obj)
    return db_obj

@router.delete("/keys/{id}")
async def delete_key(
    id: uuid.UUID,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = delete(GatewayKey).where(GatewayKey.id == id)
    result = await session.execute(stmt)
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="Key not found")
    await session.commit()
    return {"status": "success"}


@router.get("/routing")
async def get_routing_config(
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = select(RoutingConfig).limit(1)
    result = await session.execute(stmt)
    cfg = result.scalar_one_or_none()
    if not cfg:
        # Save a default
        default_yaml = "tiers:\n  lite:\n    description: Fastest models\n"
        cfg = RoutingConfig(id=uuid.uuid4(), yaml_content=default_yaml, updated_at=datetime.datetime.utcnow())
        session.add(cfg)
        await session.commit()
    return {"yaml_content": cfg.yaml_content}

@router.post("/routing")
async def update_routing_config(
    config: RoutingConfigUpdate,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    # validate
    try:
        parsed = yaml.safe_load(config.yaml_content)
        if not isinstance(parsed, dict):
            raise ValueError("YAML must be a dictionary")
        if "tiers" not in parsed:
            raise ValueError("Missing 'tiers' root node")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {str(e)}")

    stmt = select(RoutingConfig).limit(1)
    result = await session.execute(stmt)
    cfg = result.scalar_one_or_none()
    
    if cfg:
        cfg.yaml_content = config.yaml_content
        cfg.updated_at = datetime.datetime.utcnow()
    else:
        cfg = RoutingConfig(id=uuid.uuid4(), yaml_content=config.yaml_content, updated_at=datetime.datetime.utcnow())
        session.add(cfg)
        
    await session.commit()
    return {"status": "success"}


@router.get("/logs")
async def get_logs(
    limit: int = 50,
    offset: int = 0,
    search: Optional[str] = None,
    provider: Optional[str] = None,
    tier: Optional[str] = None,
    status: Optional[str] = None,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    from sqlalchemy import or_, func
    stmt_base = select(RequestLog)
    
    filters = []
    if provider:
        filters.append(RequestLog.provider == provider)
    if status:
        if status == "success":
            filters.append(RequestLog.status.like("success%"))
        else:
            filters.append(RequestLog.status.not_like("success%"))
    if tier:
        filters.append(RequestLog.model_alias == tier)
    if search:
        search_filter = f"%{search}%"
        filters.append(or_(
            RequestLog.prompt_json.ilike(search_filter),
            RequestLog.response_text.ilike(search_filter)
        ))
        
    if filters:
        stmt_base = stmt_base.where(*filters)
        
    count_stmt = select(func.count(RequestLog.id))
    if filters:
        count_stmt = count_stmt.where(*filters)
        
    # Get total records
    total_records = await session.execute(count_stmt)
    total = total_records.scalar_one()

    # Get page records
    stmt = stmt_base.order_by(RequestLog.created_at.desc()).offset(offset).limit(limit)
    result = await session.execute(stmt)
    records = result.scalars().all()
    
    return {
        "total": total,
        "page": offset // limit + 1,
        "items": [
            {
                "id": r.id,
                "client_key_id": r.client_key_id,
                "model_alias": r.model_alias,
                "actual_model": r.actual_model,
                "provider": r.provider,
                "prompt_tokens": r.prompt_tokens,
                "completion_tokens": r.completion_tokens,
                "cost_usd": r.cost_usd,
                "latency_ms": r.latency_ms,
                "status": r.status,
                "created_at": (
                    r.created_at.replace(tzinfo=None).isoformat() + "Z"
                    if r.created_at.tzinfo is not None
                    else r.created_at.isoformat() + "Z"
                ),
                "prompt_json": r.prompt_json,
                "response_text": r.response_text
            } for r in records
        ]
    }

@router.get("/models", response_model=List[ModelResponse])
async def list_admin_models(
    key: GatewayKey = Depends(require_admin_key), 
    session: AsyncSession = Depends(get_db_session)
):
    from shared.models import ProviderModel
    stmt = select(ProviderModel).order_by(ProviderModel.model_id)
    result = await session.execute(stmt)
    return result.scalars().all()

@router.post("/models", response_model=ModelResponse)
async def create_admin_model(
    model_data: ModelCreate,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    from shared.models import ProviderModel
    data = model_data.model_dump()
    model_db = ProviderModel(
        provider_id=data["provider_id"],
        model_id=data["model_id"],
        display_name=data.get("model_id", ""),   # use model_id as display name fallback
        context_window=128000,
        input_cost_per_1k=0.0,
        output_cost_per_1k=0.0,
        tier=data.get("tier") or "",
        enabled=data.get("enabled", True),
    )
    session.add(model_db)
    await session.commit()
    await session.refresh(model_db)
    return model_db

class RevealKeyRequest(BaseModel):
    password: Optional[str] = None

@router.post("/keys/{key_id}/reveal")
async def reveal_key(
    key_id: UUID,
    req: RevealKeyRequest,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    import os
    admin_password = unwrap_secret(os.environ.get("MASTER_PASSWORD") or os.environ.get("ADMIN_PASSWORD", "admin"))
    if req.password != admin_password:
        raise HTTPException(status_code=401, detail="Invalid master password")

    result = await session.execute(select(GatewayKey).where(GatewayKey.id == key_id))
    db_key = result.scalar_one_or_none()
    if not db_key:
        raise HTTPException(status_code=404, detail="Key not found")
    
    # Return the raw token if available, otherwise just part of the hash
    reveal_info = db_key.raw_token if hasattr(db_key, "raw_token") and db_key.raw_token else f"sk-...{db_key.key_hash[:8]}"
    return {"reveal_info": reveal_info}

@router.get("/logs/stats", response_model=LogStatsResponse)
async def log_stats(
    hours: int = 24,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    from sqlalchemy import func, text, Integer
    import datetime
    
    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)
    
    stmt = select(
        func.count(RequestLog.id).label("total"),
        func.sum(func.cast(RequestLog.status != "success", Integer)).label("errors"),
        func.avg(RequestLog.latency_ms).label("avg_latency"),
        func.sum(RequestLog.prompt_tokens).label("prompt_tokens"),
        func.sum(RequestLog.completion_tokens).label("completion_tokens")
    ).where(RequestLog.created_at >= since)
    
    res = await session.execute(stmt)
    row = res.fetchone()
    
    if not row or not row.total:
        return LogStatsResponse(
            total_requests=0, error_rate_percent=0.0, avg_latency_ms=0,
            total_prompt_tokens=0, total_completion_tokens=0
        )
        
    total = row.total
    errors = row.errors or 0
    
    return LogStatsResponse(
        total_requests=total,
        error_rate_percent=round((errors / total) * 100, 2) if total > 0 else 0.0,
        avg_latency_ms=int(row.avg_latency) if row.avg_latency else 0,
        total_prompt_tokens=int(row.prompt_tokens) if row.prompt_tokens else 0,
        total_completion_tokens=int(row.completion_tokens) if row.completion_tokens else 0
    )

@router.get("/usage", response_model=UsageStatsResponse)
async def usage_stats(
    days: int = 30,
    provider: Optional[str] = None,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    from sqlalchemy import func
    import datetime

    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)
    
    query = select(
        RequestLog.provider,
        func.sum(RequestLog.prompt_tokens).label("prompt_tokens"),
        func.sum(RequestLog.completion_tokens).label("completion_tokens"),
        func.sum(RequestLog.cost_usd).label("cost_usd"),
        func.count(RequestLog.id).label("request_count")
    ).where(RequestLog.created_at >= since, RequestLog.status.like("success%"))
    
    if provider:
        query = query.where(RequestLog.provider == provider)
        
    stmt = query.group_by(RequestLog.provider).order_by(func.sum(RequestLog.cost_usd).desc())
    
    res = await session.execute(stmt)
    rows = res.fetchall()
    
    items = []
    total_cost = 0.0
    total_reqs = 0
    
    for row in rows:
        p_tokens = int(row.prompt_tokens) if row.prompt_tokens else 0
        c_tokens = int(row.completion_tokens) if row.completion_tokens else 0
        cost = float(row.cost_usd) if row.cost_usd else 0.0
        reqs = int(row.request_count) or 0
        
        items.append(ProviderUsageResponse(
            provider=row.provider,
            prompt_tokens=p_tokens,
            completion_tokens=c_tokens,
            total_tokens=p_tokens + c_tokens,
            cost_usd=round(cost, 4),
            request_count=reqs
        ))
        
        total_cost += cost
        total_reqs += reqs
        
    return UsageStatsResponse(
        items=items,
        total_cost=round(total_cost, 4),
        total_requests=total_reqs
    )

@router.get("/usage/details")
async def usage_details(
    days: int = 30,
    provider: Optional[str] = None,
    credential_id: Optional[str] = None,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    """Detailed usage drilldown: provider → credential → model."""
    from sqlalchemy import func
    import datetime

    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)

    query = select(
        RequestLog.provider,
        RequestLog.credential_id,
        RequestLog.actual_model,
        func.sum(RequestLog.prompt_tokens).label("prompt_tokens"),
        func.sum(RequestLog.completion_tokens).label("completion_tokens"),
        func.sum(RequestLog.cost_usd).label("cost_usd"),
        func.count(RequestLog.id).label("request_count")
    ).where(RequestLog.created_at >= since, RequestLog.status.like("success%"))

    if provider:
        query = query.where(RequestLog.provider == provider)
    if credential_id:
        from uuid import UUID as _UUID
        query = query.where(RequestLog.credential_id == _UUID(credential_id))

    stmt = query.group_by(
        RequestLog.provider,
        RequestLog.credential_id,
        RequestLog.actual_model
    ).order_by(func.sum(RequestLog.cost_usd).desc())

    res = await session.execute(stmt)
    rows = res.fetchall()

    # Resolve credential labels in bulk
    cred_ids = {r.credential_id for r in rows if r.credential_id}
    cred_labels = {}
    if cred_ids:
        cred_stmt = select(Credential.id, Credential.label).where(Credential.id.in_(cred_ids))
        cred_res = await session.execute(cred_stmt)
        cred_labels = {str(row.id): row.label for row in cred_res}

    items = []
    total_cost = 0.0
    total_reqs = 0

    for row in rows:
        p_tokens = int(row.prompt_tokens) if row.prompt_tokens else 0
        c_tokens = int(row.completion_tokens) if row.completion_tokens else 0
        cost = float(row.cost_usd) if row.cost_usd else 0.0
        reqs = int(row.request_count) or 0
        cid = str(row.credential_id) if row.credential_id else None

        items.append({
            "provider": row.provider,
            "credential_id": cid,
            "credential_label": cred_labels.get(cid, "Unknown") if cid else "N/A",
            "actual_model": row.actual_model,
            "prompt_tokens": p_tokens,
            "completion_tokens": c_tokens,
            "total_tokens": p_tokens + c_tokens,
            "cost_usd": round(cost, 4),
            "request_count": reqs
        })

        total_cost += cost
        total_reqs += reqs

    return {
        "items": items,
        "total_cost": round(total_cost, 4),
        "total_requests": total_reqs
    }

@router.get("/logs/timeline", response_model=list)
async def logs_timeline(
    hours: int = 24,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    """
    Returns hourly token usage for the last N hours.
    Each item: { time, prompt_tokens, completion_tokens, total_tokens, cost_usd, requests }
    """
    from sqlalchemy import func, text
    import datetime

    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=hours)

    try:
        # PostgreSQL: use date_trunc
        stmt = (
            select(
                func.date_trunc("hour", RequestLog.created_at).label("hour"),
                func.sum(RequestLog.prompt_tokens).label("prompt_tokens"),
                func.sum(RequestLog.completion_tokens).label("completion_tokens"),
                func.sum(RequestLog.cost_usd).label("cost_usd"),
                func.count(RequestLog.id).label("requests"),
            )
            .where(RequestLog.created_at >= since, RequestLog.status.like("success%"))
            .group_by(text("hour"))
            .order_by(text("hour"))
        )
        res = await session.execute(stmt)
        rows = res.fetchall()
    except Exception:
        # SQLite fallback: strftime
        stmt = (
            select(
                func.strftime("%Y-%m-%dT%H:00:00", RequestLog.created_at).label("hour"),
                func.sum(RequestLog.prompt_tokens).label("prompt_tokens"),
                func.sum(RequestLog.completion_tokens).label("completion_tokens"),
                func.sum(RequestLog.cost_usd).label("cost_usd"),
                func.count(RequestLog.id).label("requests"),
            )
            .where(RequestLog.created_at >= since, RequestLog.status.like("success%"))
            .group_by(text("hour"))
            .order_by(text("hour"))
        )
        res = await session.execute(stmt)
        rows = res.fetchall()

    result = []
    for row in rows:
        try:
            hour_str = row.hour.strftime("%H:%M") if hasattr(row.hour, "strftime") else str(row.hour)[11:16]
        except Exception:
            hour_str = str(row.hour)
        pt = int(row.prompt_tokens or 0)
        ct = int(row.completion_tokens or 0)
        result.append({
            "time": hour_str,
            "prompt_tokens": pt,
            "completion_tokens": ct,
            "total_tokens": pt + ct,
            "cost_usd": round(float(row.cost_usd or 0), 6),
            "requests": int(row.requests or 0),
        })
    return result
