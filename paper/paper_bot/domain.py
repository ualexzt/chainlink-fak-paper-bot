from __future__ import annotations

from enum import Enum


class Asset(str, Enum):
    BTC = "btc"
    ETH = "eth"
    SOL = "sol"
