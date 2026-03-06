"""Configuration loading from config.toml."""

from __future__ import annotations

import tomllib
from pathlib import Path

from pydantic import BaseModel, Field, field_validator, model_validator

DEFAULT_CONFIG_PATH = Path(__file__).parent.parent.parent / "config.toml"


class IBKRConfig(BaseModel):
    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=7497, ge=1, le=65535)
    client_id: int = Field(default=1, ge=0)
    timeout: int = Field(default=30, gt=0)
    account: str = ""

    @field_validator("account")
    @classmethod
    def _normalize_account(cls, value: str) -> str:
        return value.strip()


class ClaudeConfig(BaseModel):
    model: str = "claude-opus-4-6"
    max_tokens: int = Field(default=4096, gt=0)
    backend: str = Field(
        default="cli"
    )  # "cli" (Claude Max via claude CLI) or "api" (ANTHROPIC_API_KEY)

    @field_validator("backend")
    @classmethod
    def _validate_backend(cls, value: str) -> str:
        backend = value.strip().lower()
        if backend not in {"cli", "api"}:
            raise ValueError("backend must be either 'cli' or 'api'")
        return backend


class RiskConfig(BaseModel):
    max_position_pct: float = Field(default=40.0, gt=0, le=100)
    max_daily_loss_pct: float = Field(default=5.0, gt=0, le=100)
    max_leverage: float = Field(default=2.0, ge=1.0)
    require_stop_loss: bool = True
    paper_trading: bool = True


class WatchlistConfig(BaseModel):
    symbols: list[str] = Field(default_factory=lambda: ["SPY", "QQQ", "XLE", "XLF", "AAPL"])


class DatabaseConfig(BaseModel):
    path: str = "data/trades.db"

    @field_validator("path")
    @classmethod
    def _validate_path(cls, value: str) -> str:
        path = value.strip()
        if not path:
            raise ValueError("database path must not be empty")
        return path


class AppConfig(BaseModel):
    ibkr: IBKRConfig = Field(default_factory=IBKRConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    risk: RiskConfig = Field(default_factory=RiskConfig)
    watchlist: WatchlistConfig = Field(default_factory=WatchlistConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)

    @model_validator(mode="after")
    def _validate_live_account(self) -> AppConfig:
        if not self.risk.paper_trading and not self.ibkr.account:
            raise ValueError("ibkr.account is required when risk.paper_trading is false")
        return self


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from a TOML file. Falls back to defaults if file missing."""
    config_path = path or DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return AppConfig()
    with open(config_path, "rb") as f:
        data = tomllib.load(f)
    return AppConfig(**data)
