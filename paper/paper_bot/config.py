from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Mapping

FORBIDDEN_ENV_PARTS = ("PRIVATE_KEY", "API_KEY", "API_SECRET", "PASSPHRASE", "CREDENTIAL")


@dataclass(frozen=True)
class Settings:
    symbols: tuple[str, ...]
    thresholds: tuple[Decimal, ...]
    paper_notional_usd: Decimal
    rtds_stale_seconds: Decimal
    data_dir: Path
    gamma_url: str
    market_ws_url: str
    rtds_url: str


def load_settings(env: Mapping[str, str]) -> Settings:
    forbidden = sorted(key for key in env if any(part in key.upper() for part in FORBIDDEN_ENV_PARTS))
    if forbidden:
        raise ValueError("forbidden credential environment keys: " + ",".join(forbidden))

    return Settings(
        symbols=tuple(x.strip().lower() for x in env.get("SYMBOLS", "btc,eth,sol").split(",")),
        thresholds=tuple(Decimal(x) for x in env.get("ENTRY_THRESHOLDS", "0.80,0.85,0.89,0.90").split(",")),
        paper_notional_usd=Decimal(env.get("PAPER_NOTIONAL_USD", "5.00")),
        rtds_stale_seconds=Decimal(env.get("RTDS_STALE_SEC", "10")),
        data_dir=Path(env.get("DATA_DIR", "/data")),
        gamma_url="https://gamma-api.polymarket.com",
        market_ws_url="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        rtds_url="wss://ws-live-data.polymarket.com",
    )
