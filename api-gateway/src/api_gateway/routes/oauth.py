import logging
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request, Query, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, delete
import secrets
import time
import os
import uuid
import jwt
import httpx
from datetime import datetime, timezone, timedelta
from typing import Dict, Any

from shared.database import get_db_session
from shared.models import Provider, Credential, GatewayKey
from shared.security import encrypt_secret

from api_gateway.auth import get_current_key, require_admin_key

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth", tags=["OAuth"])

# ── Google Antigravity (gemini-cli) credentials ──
_ANTIGRAVITY_CLIENT_ID = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
_ANTIGRAVITY_CLIENT_SECRET = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
_ANTIGRAVITY_SCOPES = " ".join([
    "https://www.googleapis.com/auth/cloud-platform",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "openid",
])
_ANTIGRAVITY_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_ANTIGRAVITY_TOKEN_URL = "https://oauth2.googleapis.com/token"

# In-flight state → (code_verifier, provider_id)
_oauth_states: Dict[str, tuple] = {}

def _pkce_pair():
    import hashlib, base64
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


_CODE_ASSIST_ENDPOINT = "https://cloudcode-pa.googleapis.com"


async def _discover_project_id(access_token: str) -> str | None:
    """
    Discovers the GCP project ID associated with the OAuth token via the
    Cloud Code Assist API. This project ID must be sent as x-goog-user-project
    when calling generativelanguage.googleapis.com with a cloud-platform token.
    Mirrors the Gemini CLI's discoverProject() function.
    """
    try:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{_CODE_ASSIST_ENDPOINT}/v1internal:loadCodeAssist",
                headers=headers,
                json={"metadata": {"ideType": "ANTIGRAVITY", "pluginType": "GEMINI"}},
            )
            if resp.status_code == 200:
                data = resp.json()
                project = data.get("cloudaicompanionProject")
                if isinstance(project, str) and project:
                    return project
                if isinstance(project, dict) and project.get("id"):
                    return project["id"]
    except Exception:
        pass
    return None


@router.get("/google-antigravity/start")
async def antigravity_start(
    request: Request,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    import urllib.parse

    if not _ANTIGRAVITY_CLIENT_ID or not _ANTIGRAVITY_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail=(
                "Google OAuth is not configured. Set GOOGLE_OAUTH_CLIENT_ID and "
                "GOOGLE_OAUTH_CLIENT_SECRET."
            ),
        )

    # 1. Find or create the "google-antigravity" provider
    stmt = select(Provider).where(Provider.name == "google-antigravity")
    result = await session.execute(stmt)
    provider = result.scalar_one_or_none()
    
    if not provider:
        provider = Provider(
            id=uuid.uuid4(),
            name="google-antigravity",
            display_name="Google Antigravity",
            auth_type="oauth2",
            enabled=True
        )
        session.add(provider)
        await session.commit()
        await session.refresh(provider)

    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _pkce_pair()
    
    _oauth_states[state] = (code_verifier, str(provider.id))
    
    netloc = request.base_url.netloc
    redirect_uri = f"{request.base_url.scheme}://{netloc}/api/oauth/google-antigravity/callback"

    params = {
        "client_id": _ANTIGRAVITY_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": _ANTIGRAVITY_SCOPES,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
        "state": state
    }
    
    url = f"{_ANTIGRAVITY_AUTH_URL}?{urllib.parse.urlencode(params)}"
    logger.info("Google Antigravity OAuth started for provider=%s", provider.id)
    return {"oauth_url": url}

@router.get("/google-antigravity/callback")
async def antigravity_callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    session: AsyncSession = Depends(get_db_session)
):
    # Standard PKCE callback logic
    if error:
        return _oauth_error_html(f"OAuth Error: {error}")
        
    if not code or not state:
        return _oauth_error_html("Missing code or state")
        
    if state not in _oauth_states:
        return _oauth_error_html("Invalid or expired state token. Please try again.")
        
    code_verifier, provider_id_str = _oauth_states.pop(state)
    netloc = request.base_url.netloc
    redirect_uri = f"{request.base_url.scheme}://{netloc}/api/oauth/google-antigravity/callback"

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            _ANTIGRAVITY_TOKEN_URL,
            data={
                "client_id": _ANTIGRAVITY_CLIENT_ID,
                "client_secret": _ANTIGRAVITY_CLIENT_SECRET,
                "code": code,
                "code_verifier": code_verifier,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code"
            }
        )
        if token_resp.status_code != 200:
            logger.error("Google Antigravity token exchange failed: %s", token_resp.text)
            return _oauth_error_html(f"Failed to exchange token: {token_resp.text}")
            
        token_data = token_resp.json()

    # Extract ID Token for email
    id_token = token_data.get("id_token")
    email = None
    name = None
    if id_token:
        try:
            payload = jwt.decode(id_token, options={"verify_signature": False})
            email = payload.get("email")
            name = payload.get("name")
        except Exception:
            pass

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")
    expires_in = int(token_data.get("expires_in", 3600))

    if email: token_data["email"] = email
    if name: token_data["name"] = name

    token_data["_unifyroute_issued_at"] = time.time()
    # Store OAuth client details so the credential vault refresh job can use them
    token_data["client_id"] = _ANTIGRAVITY_CLIENT_ID
    token_data["client_secret"] = _ANTIGRAVITY_CLIENT_SECRET
    token_data["token_url"] = _ANTIGRAVITY_TOKEN_URL

    # Discover GCP project ID — required as x-goog-user-project header in generateContent
    # calls using cloud-platform scoped OAuth tokens.
    project_id = await _discover_project_id(access_token)
    if project_id:
        token_data["project_id"] = project_id

    label = f"Google Antigravity — {email or name or 'unknown'}"

    iv = None
    try:
        from shared.security import encrypt_secret
        token_to_encrypt = access_token or refresh_token
        if token_to_encrypt:
            secret_enc, iv = encrypt_secret(token_to_encrypt)
        else:
            secret_enc = b"MISSING_TOKEN"
    except Exception:
        secret_enc = b"STORED_IN_OAUTH_META"

    # Calculate token expiry so the credential vault refresh job fires before it expires
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

    cred = Credential(
        id=uuid.uuid4(),
        provider_id=uuid.UUID(provider_id_str),
        label=label,
        auth_type="oauth2",
        secret_enc=secret_enc,
        iv=iv,
        oauth_meta=token_data,
        expires_at=expires_at,
        enabled=True,
    )
    session.add(cred)
    await session.commit()
    logger.info("Google Antigravity credential created: label=%s email=%s", label, email)

    return _oauth_success_html("Antigravity Authentication Successful")

def _oauth_success_html(message: str) -> HTMLResponse:
    html = f"""
    <html>
    <body>
      <h2>{message}</h2>
      <p>You can close this window now.</p>
      <script>
        if (window.opener) {{
            window.opener.postMessage({{ type: 'oauth_success' }}, '*');
            window.close();
        }}
      </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)

def _oauth_error_html(message: str) -> HTMLResponse:
    html = f"""
    <html>
    <body>
      <h2 style="color:red">{message}</h2>
      <p>Please close this window and try again.</p>
    </body>
    </html>
    """
    return HTMLResponse(content=html, status_code=400)

@router.get("/start/{provider_id}")
async def oauth_start(
    provider_id: uuid.UUID,
    request: Request,
    key: GatewayKey = Depends(require_admin_key),
    session: AsyncSession = Depends(get_db_session)
):
    stmt = select(Provider).where(Provider.id == provider_id)
    result = await session.execute(stmt)
    provider = result.scalar_one_or_none()
    
    if not provider or provider.auth_type != "oauth2":
        raise HTTPException(status_code=400, detail="Provider not found or not OAuth2")
        
    meta = provider.oauth_meta or {}
    auth_url = meta.get("auth_url")
    client_id = meta.get("client_id")
    
    if not auth_url or not client_id:
        raise HTTPException(status_code=400, detail="Provider missing auth_url or client_id in config")

    import urllib.parse
    state = secrets.token_urlsafe(32)
    _oauth_states[state] = (None, str(provider.id)) 
    
    netloc = request.base_url.netloc
    redirect_uri = f"{request.base_url.scheme}://{netloc}/api/oauth/callback"
    
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "state": state
    }
    
    scope = meta.get("scope")
    if scope:
        params["scope"] = scope
        
    url = f"{auth_url}?{urllib.parse.urlencode(params)}"
    return {"oauth_url": url}

@router.get("/callback")
async def oauth_callback(
    request: Request,
    code: str = Query(None),
    state: str = Query(None),
    error: str = Query(None),
    session: AsyncSession = Depends(get_db_session)
):
    if error:
        return _oauth_error_html(f"OAuth Error: {error}")
        
    if not code or not state:
        return _oauth_error_html("Missing code or state")
        
    if state not in _oauth_states:
        return _oauth_error_html("Invalid or expired state token.")
        
    _, provider_id_str = _oauth_states.pop(state)
    
    stmt = select(Provider).where(Provider.id == uuid.UUID(provider_id_str))
    result = await session.execute(stmt)
    provider = result.scalar_one_or_none()
    
    if not provider:
         return _oauth_error_html("OAuth Provider disappeared.")
         
    meta = provider.oauth_meta or {}
    token_url = meta.get("token_url")
    client_id = meta.get("client_id")
    client_secret = meta.get("client_secret")
    
    netloc = request.base_url.netloc
    redirect_uri = f"{request.base_url.scheme}://{netloc}/api/oauth/callback"

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            token_url,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code"
            }
        )
        if token_resp.status_code != 200:
            logger.error("Generic OAuth token exchange failed for provider=%s: %s", provider_id_str, token_resp.text)
            return _oauth_error_html(f"Failed to exchange token: {token_resp.text}")
            
        token_data = token_resp.json()

    access_token = token_data.get("access_token", "")

    from shared.security import encrypt_secret
    iv = None
    try:
        if access_token:
            secret_enc, iv = encrypt_secret(access_token)
        else:
            secret_enc = b"MISSING_TOKEN"
    except Exception:
        secret_enc = b"STORED_IN_OAUTH_META"

    cred = Credential(
        id=uuid.uuid4(),
        provider_id=provider.id,
        label=f"{provider.display_name} OAuth token",
        auth_type="oauth2",
        secret_enc=secret_enc,
        iv=iv,
        oauth_meta=token_data,
        enabled=True,
    )
    session.add(cred)
    await session.commit()
    logger.info("Generic OAuth credential created: provider=%s label=%s", provider.name, cred.label)

    return _oauth_success_html("Authentication Successful")
