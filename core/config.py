"""Central configuration using Pydantic-Settings. All values come from .env."""

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app_env values that mean "trading real or paper capital, unattended" — as
# opposed to "development", where a throwaway local SQLite file is fine.
_PRODUCTION_LIKE_ENVS = frozenset({"paper", "live"})


class SwarmSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Runtime
    app_env: str = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = Field(default="sqlite+aiosqlite:///./swarm_trading.db")
    supabase_url: str = ""
    supabase_key: str = ""

    # Redis
    redis_url: str = "redis://localhost:6379/0"

    # Brokers
    ibkr_host: str = "127.0.0.1"
    ibkr_port: int = 7497
    ibkr_client_id: int = 1
    mt5_login: int = 0
    mt5_password: str = ""
    mt5_server: str = ""

    # Data feeds
    polygon_api_key: str = ""
    yfinance_enabled: bool = True

    # News
    newsapi_key: str = ""
    forex_factory_enabled: bool = True

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Swarm
    swarm_total_capital_usd: float = 100_000.0
    swarm_agent_count: int = 100
    swarm_capital_per_agent: float = 1_000.0
    swarm_target_multiplier: float = 10.0

    # Risk (FTMO-style)
    risk_max_total_loss_pct: float = 0.50
    risk_max_agents_per_symbol: int = 10
    risk_news_blackout_min: int = 5
    risk_min_entry_pct: float = 0.03
    risk_max_entry_pct: float = 0.15

    # Dashboard
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000

    # RL agents (agents/rl)
    # rl_model_dir is where RLAgent loads checkpoints from (inference only —
    # see docs/architecture/adr/0001-rl-inference-only-in-production.md) and
    # where the offline `agents/rl/train.py` script saves them to.
    rl_model_dir: str = "agents/rl/checkpoints"
    rl_pretrain_timesteps: int = 20_000

    @model_validator(mode="after")
    def _no_silent_sqlite_in_production(self) -> "SwarmSettings":
        """SQLite (the default) is for local dev and unit tests only — see
        docs/architecture/adr/0008-postgresql-alembic-schema-authority.md.
        A paper/live run against SQLite would silently skip Alembic-managed
        schema guarantees and concurrent-write safety, so fail loudly at
        startup instead of degrading quietly."""
        if self.app_env in _PRODUCTION_LIKE_ENVS and self.database_url.startswith("sqlite"):
            raise ValueError(
                f"DATABASE_URL must point to PostgreSQL when APP_ENV={self.app_env!r} "
                "(got a sqlite:// URL). SQLite is for local development and unit tests "
                "only. Set DATABASE_URL=postgresql+asyncpg://... in .env."
            )
        return self


settings = SwarmSettings()
