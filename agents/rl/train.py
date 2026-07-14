"""
Offline pretraining for RL agents — run once (or periodically) before
deploying RLAgent instances so they don't start from a completely
untrained policy in production.

Usage:
    python -m swarm_trading.agents.rl.train                  # all 5 symbols
    python -m swarm_trading.agents.rl.train --symbol XAUUSD  # just one
    python -m swarm_trading.agents.rl.train --timesteps 50000
"""
from __future__ import annotations
import argparse
from pathlib import Path
from loguru import logger

from swarm_trading.core.config import settings
from swarm_trading.core.models import Symbol


def train_symbol(symbol: Symbol, timesteps: int) -> Path:
    from stable_baselines3 import PPO
    from swarm_trading.agents.rl.env import SwarmTradingEnv
    from swarm_trading.agents.rl.data import fetch_feature_frame

    logger.info(f"[RL train] Fetching history for {symbol.value}...")
    df = fetch_feature_frame(symbol, period="730d", interval="1h")
    env = SwarmTradingEnv(df)

    model = PPO("MlpPolicy", env, verbose=1)
    logger.info(f"[RL train] Training {symbol.value} for {timesteps} timesteps ({len(df)} bars)...")
    model.learn(total_timesteps=timesteps)

    out_dir = Path(settings.rl_model_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{symbol.value}_ppo.zip"
    model.save(str(out_path))
    logger.success(f"[RL train] Saved {out_path}")
    return out_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Pretrain RL trading agents")
    parser.add_argument("--symbol", choices=[s.value for s in Symbol], default=None)
    parser.add_argument("--timesteps", type=int, default=settings.rl_pretrain_timesteps)
    args = parser.parse_args()

    symbols = [Symbol(args.symbol)] if args.symbol else list(Symbol)
    for symbol in symbols:
        train_symbol(symbol, args.timesteps)


if __name__ == "__main__":
    main()
