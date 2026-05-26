from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class AdminConfig(BaseModel):
    username: str = "admin"
    password: str = "admin123"


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080


class QueueConfig(BaseModel):
    max_concurrent_upstream: int = 1
    max_queue_size: int = 50


class NovelAIConfig(BaseModel):
    api_key: str = ""
    account_tier: int = Field(default=3, ge=0, le=3)
    upscale_anlas_cost: int = Field(default=0, ge=0)


class DatabaseConfig(BaseModel):
    path: str = "novelai_proxy.db"


class AppConfig(BaseModel):
    admin: AdminConfig = Field(default_factory=AdminConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    queue: QueueConfig = Field(default_factory=QueueConfig)
    novelai: NovelAIConfig = Field(default_factory=NovelAIConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        return AppConfig()
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return AppConfig.model_validate(data)
