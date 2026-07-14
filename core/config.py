"""Central configuration using Pydantic-Settings. All values come from .env."""
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    swarm_total_capital_usd: float = 1000.0
    swarm_agent_count: int = 100
    swarm_capital_per_agent: float = 10.0
    swarm_target_multiplier: float = 10.0

    # Risk (FTMO-style)
    risk_max_total_loss_pct: float = 0.50
    risk_max_agents_per_symbol: int = 10
    risk_news_blackout_min: int = 5

    # Dashboard
    dashboard_host: str = "0.0.0.0"
    dashboard_port: int = 8000

    # RL agents (agents/rl)
    rl_model_dir: str = "agents/rl/checkpoints"
    rl_retrain_every_n_trades: int = 20
    rl_pretrain_timesteps: int = 20_000
    rl_incremental_timesteps: int = 2_000


settings = SwarmSettings()
