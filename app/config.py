from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator


RESERVED_UPSTREAM_IDS = {"__all__"}
NovelAIAuthType = Literal["api_key", "login"]


class AdminConfig(BaseModel):
    username: str = "admin"
    password: str = "admin123"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class QueueConfig(BaseModel):
    max_queue_size: int = 50
    dispatch_max_queue_size: int | None = Field(default=None, ge=1)
    upstream_interval_min_seconds: float = Field(default=2, ge=0)
    upstream_interval_max_seconds: float = Field(default=5, ge=0)
    upstream_error_extra_delay_seconds: float = Field(default=5, ge=0)
    upstream_execution_timeout_seconds: float = Field(default=60, gt=0)
    retry_429_queue_length_threshold: int = Field(default=3, ge=-1)
    retry_429_max_attempts: int = Field(default=5, ge=1)

    @model_validator(mode="after")
    def validate_upstream_interval_range(self):
        if self.upstream_interval_max_seconds < self.upstream_interval_min_seconds:
            raise ValueError("queue.upstream_interval_max_seconds must be greater than or equal to upstream_interval_min_seconds")
        return self


class NovelAIUpstreamConfig(BaseModel):
    id: str
    auth_type: NovelAIAuthType = "api_key"
    api_key: str = ""
    username: str = ""
    password: str = ""
    enabled: bool = True

    @model_validator(mode="after")
    def validate_upstream(self):
        if not self.id.strip():
            raise ValueError("novelai.upstreams[].id must not be empty")
        self.id = self.id.strip()
        if self.id in RESERVED_UPSTREAM_IDS:
            raise ValueError(f"novelai.upstreams[].id is reserved: {self.id}")
        if self.enabled and self.auth_type == "login":
            if not self.username.strip():
                raise ValueError("novelai.upstreams[].username must not be empty when auth_type is login")
            if not self.password.strip():
                raise ValueError("novelai.upstreams[].password must not be empty when auth_type is login")
        return self


class NovelAIConfig(BaseModel):
    auth_type: NovelAIAuthType = "api_key"
    api_key: str = ""
    username: str = ""
    password: str = ""
    upstreams: list[NovelAIUpstreamConfig] = Field(default_factory=list)
    account_tier: int = Field(default=3, ge=0, le=3)
    upscale_anlas_cost: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_novelai(self):
        uses_top_level_upstream = not any(upstream.enabled for upstream in self.upstreams)
        if uses_top_level_upstream and self.auth_type == "login":
            if not self.username.strip():
                raise ValueError("novelai.username must not be empty when auth_type is login")
            if not self.password.strip():
                raise ValueError("novelai.password must not be empty when auth_type is login")
        ids = [upstream.id for upstream in self.upstreams]
        if len(ids) != len(set(ids)):
            raise ValueError("novelai.upstreams[].id must be unique")
        return self


class AdaptiveRoutingConfig(BaseModel):
    initial_score: float = Field(default=0.8, ge=0, le=1)
    alpha: float = Field(default=0.4, gt=0, le=1)
    min_weight: float = Field(default=0.15, ge=0)


class RoutingConfig(BaseModel):
    strategy: Literal["round_robin", "random", "adaptive_weighted_random"] = "round_robin"
    adaptive: AdaptiveRoutingConfig = Field(default_factory=AdaptiveRoutingConfig)


class PayloadArchiveConfig(BaseModel):
    enabled: bool = False
    hot_days: int = Field(default=7, ge=0, le=3650)
    compression: Literal["zlib"] = "zlib"
    compression_level: int = Field(default=6, ge=0, le=9)
    max_payloads_per_part: int = Field(default=1000, ge=1)
    max_raw_bytes_per_part: int = Field(default=33554432, ge=1024)
    run_interval_hours: float = Field(default=24, gt=0)
    max_rows_per_run: int = Field(default=10000, ge=1)


class HotPayloadConfig(BaseModel):
    enabled: bool = False
    compression: Literal["zlib"] = "zlib"
    compression_level: int = Field(default=6, ge=0, le=9)
    min_bytes: int = Field(default=4096, ge=0)
    min_savings_ratio: float = Field(default=0.10, ge=0, le=1)


class AutoVacuumConfig(BaseModel):
    enabled: bool = True
    run_time_utc8: str = "04:00"

    @field_validator("run_time_utc8")
    @classmethod
    def validate_run_time_utc8(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2:
            raise ValueError("database.auto_vacuum.run_time_utc8 must use HH:MM format")
        try:
            hour = int(parts[0])
            minute = int(parts[1])
        except ValueError as exc:
            raise ValueError("database.auto_vacuum.run_time_utc8 must use HH:MM format") from exc
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("database.auto_vacuum.run_time_utc8 must be a valid UTC+8 time")
        return f"{hour:02d}:{minute:02d}"


class DatabaseConfig(BaseModel):
    path: str = "novelai_proxy.db"
    auto_vacuum: AutoVacuumConfig = Field(default_factory=AutoVacuumConfig)
    hot_payload: HotPayloadConfig = Field(default_factory=HotPayloadConfig)
    payload_archive: PayloadArchiveConfig = Field(default_factory=PayloadArchiveConfig)


class LoggingConfig(BaseModel):
    level: Literal["DEBUG", "INFO", "ERROR"] = "INFO"
    directory: str = "logs"
    request_log_file: str = "novelai_proxy.log"
    save_generated_images: bool = True
    generated_images_dir: str = "generated_images"


class ImageFormatConfig(BaseModel):
    mode: Literal["request", "force"] = "request"
    format: Literal["png", "jpeg", "webp"] = "webp"


class CorsConfig(BaseModel):
    enabled: bool = True
    allow_origins: list[str] = Field(default_factory=lambda: ["*"])
    allow_origin_regex: str | None = None
    allow_methods: list[str] = Field(default_factory=lambda: ["*"])
    allow_headers: list[str] = Field(default_factory=lambda: ["*"])
    expose_headers: list[str] = Field(default_factory=lambda: ["Content-Disposition"])
    allow_credentials: bool = False
    max_age: int = Field(default=600, ge=0)


class CatboxConfig(BaseModel):
    api_url: str = "https://catbox.moe/user/api.php"
    userhash: str = ""


class ImageHostingConfig(BaseModel):
    enabled: bool = False
    provider: Literal["catbox"] = "catbox"
    timeout_seconds: float = Field(default=30, gt=0)
    max_pending_uploads: int = Field(default=50, ge=0)
    local_format_conversion: bool = False
    local_conversion_format: Literal["png", "jpeg", "webp"] = "webp"
    catbox: CatboxConfig = Field(default_factory=CatboxConfig)


class FreeSmallDailyLimitConfig(BaseModel):
    reset_hour_utc8: int = Field(default=0, ge=0, le=23)


class DiscordSelfServiceConfig(BaseModel):
    enabled: bool = False
    client_id: str = ""
    client_secret: str = ""
    redirect_uri: str = "http://127.0.0.1:8080/auth/discord/callback"
    required_guild_id: str = ""
    default_group_id: int | None = Field(default=None, ge=1)
    session_secret: str = ""


class SelfServiceConfig(BaseModel):
    discord: DiscordSelfServiceConfig = Field(default_factory=DiscordSelfServiceConfig)


class AppConfig(BaseModel):
    admin: AdminConfig = Field(default_factory=AdminConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    novelai: NovelAIConfig = Field(default_factory=NovelAIConfig)
    routing: RoutingConfig = Field(default_factory=RoutingConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    image_format: ImageFormatConfig = Field(default_factory=ImageFormatConfig)
    image_hosting: ImageHostingConfig = Field(default_factory=ImageHostingConfig)
    free_small_daily_limit: FreeSmallDailyLimitConfig = Field(default_factory=FreeSmallDailyLimitConfig)
    self_service: SelfServiceConfig = Field(default_factory=SelfServiceConfig)
    cors: CorsConfig = Field(default_factory=CorsConfig)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    def model_post_init(self, __context) -> None:
        if "log_level" in self.model_fields_set and "logging" not in self.model_fields_set:
            if self.log_level in {"DEBUG", "INFO", "ERROR"}:
                self.logging.level = self.log_level


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        return AppConfig()
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)
