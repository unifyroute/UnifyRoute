from datetime import datetime
from uuid import UUID, uuid4
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    BigInteger,
    CheckConstraint,
    JSON as sa_JSON,
    LargeBinary,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass

class Provider(Base):
    __tablename__ = "providers"
    __table_args__ = (
        CheckConstraint("auth_type IN ('api_key', 'oauth2')", name="check_auth_type"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(Text, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(Text)
    auth_type: Mapped[str] = mapped_column(Text)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    oauth_meta: Mapped[dict | None] = mapped_column(sa_JSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    credentials: Mapped[list["Credential"]] = relationship("Credential", back_populates="provider")
    models: Mapped[list["ProviderModel"]] = relationship("ProviderModel", back_populates="provider")



class Credential(Base):
    __tablename__ = "credentials"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider_id: Mapped[UUID] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    label: Mapped[str] = mapped_column(Text)
    auth_type: Mapped[str] = mapped_column(Text)
    secret_enc: Mapped[bytes] = mapped_column(LargeBinary)
    iv: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    oauth_meta: Mapped[dict | None] = mapped_column(sa_JSON, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str | None] = mapped_column(Text, nullable=True) # e.g. "ok", "error", "unverified"
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    provider: Mapped["Provider"] = relationship("Provider", back_populates="credentials")
    quota_snapshots: Mapped[list["QuotaSnapshot"]] = relationship("QuotaSnapshot", back_populates="credential")


class ProviderModel(Base):
    __tablename__ = "provider_models"
    __table_args__ = (
        CheckConstraint("tier IN ('lite', 'base', 'thinking', '')", name="check_tier"),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider_id: Mapped[UUID] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    model_id: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    context_window: Mapped[int] = mapped_column(Integer)
    input_cost_per_1k: Mapped[float] = mapped_column(Numeric(10, 6))
    output_cost_per_1k: Mapped[float] = mapped_column(Numeric(10, 6))
    tier: Mapped[str] = mapped_column(Text)
    supports_streaming: Mapped[bool] = mapped_column(Boolean, default=True)
    supports_functions: Mapped[bool] = mapped_column(Boolean, default=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    provider: Mapped["Provider"] = relationship("Provider", back_populates="models")


class QuotaSnapshot(Base):
    __tablename__ = "quota_snapshots"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    credential_id: Mapped[UUID] = mapped_column(ForeignKey("credentials.id", ondelete="CASCADE"))
    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)  # Null if instance-level/credential-level quota
    tokens_remaining: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    requests_remaining: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resets_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    polled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    credential: Mapped["Credential"] = relationship("Credential", back_populates="quota_snapshots")


class RequestLog(Base):
    __tablename__ = "request_logs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    client_key_id: Mapped[UUID | None] = mapped_column(ForeignKey("gateway_keys.id", ondelete="SET NULL"), nullable=True)
    credential_id: Mapped[UUID | None] = mapped_column(ForeignKey("credentials.id", ondelete="SET NULL"), nullable=True)
    model_alias: Mapped[str] = mapped_column(Text)
    actual_model: Mapped[str] = mapped_column(Text)
    provider: Mapped[str] = mapped_column(Text)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(10, 6), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    
    # New prompt/response storage
    prompt_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    gateway_key: Mapped["GatewayKey"] = relationship("GatewayKey", back_populates="requests")
    credential: Mapped["Credential"] = relationship("Credential")


class GatewayKey(Base):
    __tablename__ = "gateway_keys"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    label: Mapped[str] = mapped_column(Text)
    key_hash: Mapped[str] = mapped_column(Text, unique=True, index=True)
    raw_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    scopes: Mapped[list[str]] = mapped_column(sa_JSON, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    rate_limit_rpm: Mapped[int | None] = mapped_column(Integer, nullable=True)

    requests: Mapped[list["RequestLog"]] = relationship("RequestLog", back_populates="gateway_key")


class BrainConfig(Base):
    """Tracks which provider/credential/model triples the Brain module may use
    for internal LLMWay system management (not for external user traffic)."""
    __tablename__ = "brain_configs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    provider_id: Mapped[UUID] = mapped_column(ForeignKey("providers.id", ondelete="CASCADE"))
    credential_id: Mapped[UUID] = mapped_column(ForeignKey("credentials.id", ondelete="CASCADE"))
    model_id: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower = higher priority
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    provider: Mapped["Provider"] = relationship("Provider")
    credential: Mapped["Credential"] = relationship("Credential")


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    topic: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    messages: Mapped[list["ChatMessage"]] = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan", order_by="ChatMessage.created_at")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    session_id: Mapped[UUID] = mapped_column(ForeignKey("chat_sessions.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(Text)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    session: Mapped["ChatSession"] = relationship("ChatSession", back_populates="messages")


class RoutingConfig(Base):
    __tablename__ = "routing_configs"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    yaml_content: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

