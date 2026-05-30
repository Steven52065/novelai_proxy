from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator


class AdminConfig(BaseModel):
    username: str = "admin"
    password: str = "admin123"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class QueueConfig(BaseModel):
    max_concurrent_upstream: int = 1
    max_queue_size: int = 50
    upstream_interval_min_seconds: float = Field(default=2, ge=0)
    upstream_interval_max_seconds: float = Field(default=5, ge=0)
    upstream_error_extra_delay_seconds: float = Field(default=5, ge=0)

    @model_validator(mode="after")
    def validate_upstream_interval_range(self):
        if self.upstream_interval_max_seconds < self.upstream_interval_min_seconds:
            raise ValueError("queue.upstream_interval_max_seconds must be greater than or equal to upstream_interval_min_seconds")
        return self


class NovelAIConfig(BaseModel):
    api_key: str = ""
    account_tier: int = Field(default=3, ge=0, le=3)
    upscale_anlas_cost: int = Field(default=0, ge=0)


class DatabaseConfig(BaseModel):
    path: str = "novelai_proxy.db"


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
    catbox: CatboxConfig = Field(default_factory=CatboxConfig)


class AppConfig(BaseModel):
    admin: AdminConfig = Field(default_factory=AdminConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    novelai: NovelAIConfig = Field(default_factory=NovelAIConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    image_format: ImageFormatConfig = Field(default_factory=ImageFormatConfig)
    image_hosting: ImageHostingConfig = Field(default_factory=ImageHostingConfig)
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
