from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class APIModelBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProviderCreate(APIModelBase):
    name: str
    display_name: str
    auth_type: str = Field(pattern="^(api_key|oauth2)$")
    base_url: Optional[str] = None
    enabled: bool = True

class ProviderUpdate(APIModelBase):
    name: Optional[str] = None
    display_name: Optional[str] = None
    auth_type: Optional[str] = Field(None, pattern="^(api_key|oauth2)$")
    base_url: Optional[str] = None
    enabled: Optional[bool] = None

class ProviderResponse(ProviderCreate):
    id: UUID
    created_at: datetime


class CredentialCreate(APIModelBase):
    provider_id: UUID
    label: str
    auth_type: str = "api_key"
    secret_key: Optional[str] = None
    enabled: bool = True
    oauth_meta: Optional[Dict[str, Any]] = None
    expires_at: Optional[datetime] = None

class CredentialUpdate(APIModelBase):
    label: Optional[str] = None
    secret_key: Optional[str] = None
    enabled: Optional[bool] = None
    status: Optional[str] = None
    error_message: Optional[str] = None
    oauth_meta: Optional[Dict[str, Any]] = None
    expires_at: Optional[datetime] = None

class CredentialResponse(APIModelBase):
    id: UUID
    provider_id: UUID
    label: str
    auth_type: str
    enabled: bool
    status: Optional[str] = None
    error_message: Optional[str] = None
    oauth_meta: Optional[Dict[str, Any]] = None
    expires_at: Optional[datetime] = None


class ProviderModelCreate(APIModelBase):
    provider_id: UUID
    model_id: str
    display_name: str
    context_window: int
    input_cost_per_1k: float
    output_cost_per_1k: float
    tier: str = Field(pattern="^(lite|base|thinking|)$")
    supports_streaming: bool = True
    enabled: bool = True

class ProviderModelResponse(ProviderModelCreate):
    id: UUID

class ModelCreate(ProviderModelCreate):
    pass

class ModelUpdate(APIModelBase):
    display_name: Optional[str] = None
    context_window: Optional[int] = None
    input_cost_per_1k: Optional[float] = None
    output_cost_per_1k: Optional[float] = None
    tier: Optional[str] = Field(None, pattern="^(lite|base|thinking|)$")
    supports_streaming: Optional[bool] = None
    enabled: Optional[bool] = None

class ModelResponse(ProviderModelResponse):
    pass

class GatewayKeyCreate(APIModelBase):
    label: str
    scopes: List[str] = Field(default_factory=list)
    enabled: bool = True

class GatewayKeyUpdate(APIModelBase):
    label: Optional[str] = None
    scopes: Optional[List[str]] = None
    enabled: Optional[bool] = None

class GatewayKeyResponse(GatewayKeyCreate):
    id: UUID
    
class GatewayKeyCreateResponse(GatewayKeyResponse):
    key_plaintext: str

class RoutingConfigUpdate(APIModelBase):
    yaml_content: str

class LogStatsResponse(APIModelBase):
    total_requests: int
    error_rate_percent: float
    avg_latency_ms: int
    total_prompt_tokens: int
    total_completion_tokens: int

class ProviderUsageResponse(APIModelBase):
    provider: Optional[str] = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    request_count: int = 0

class UsageStatsResponse(APIModelBase):
    items: List[ProviderUsageResponse]
    total_cost: float
    total_requests: int


class LogResponse(APIModelBase):
    id: UUID
    client_key_id: Optional[UUID] = None
    model_alias: str
    actual_model: str
    provider: str
    prompt_tokens: Optional[int] = None
    completion_tokens: Optional[int] = None
    cost_usd: Optional[float] = None
    latency_ms: Optional[int] = None
    status: str
    created_at: datetime
    prompt_json: Optional[str] = None
    response_text: Optional[str] = None

class ChatMessage(BaseModel):
    role: str
    content: Any = None
    model_config = ConfigDict(extra="allow")
    
class ChatCompletionRequest(BaseModel):
    model: str
    messages: List[ChatMessage]
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    session_id: Optional[UUID] = None
    model_config = ConfigDict(extra="allow")


class CompletionRequest(BaseModel):
    model: str
    prompt: Any  # Usually str, but some providers support lists of strings/tokens
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = False
    model_config = ConfigDict(extra="allow")

ChatRequest = ChatCompletionRequest
