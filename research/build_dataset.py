"""Build training dataset joining Gamma market outcomes with Binance 1s price paths.

Resolution model (from Polymarket cryptoMarketConfig 'twap-60'):
  strike = 60s TWAP at window start, final = 60s TWAP at window end,
  Up wins if final >= strike. We reconstruct with Binance 1s closes as proxy
  for Chainlink and validate against actual outcomePrices.

One row = (market, decision time T): features known at T, label = whether the
leading side at T ultimately won.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/home/alex/Project/up_down")
GAMMA = ROOT / "data" / "gamma"
KLINES = ROOT / "data" / "binance"

DECISION_TIMES = list(range(60, 901, 30))   # seconds after window start


def load_kline_day(symbol: str, day0: int) -> pd.DataFrame | None:
    name = time_gmtime(day0)
    f = KLINES / symbol / f"{name}.parquet"
    if not f.exists():
        return None
    df = pd.read_parquet(f)
    df["close"] = df["close"].astype(float)
    df["sec"] = df["open_time"] // 1000
    # forward-fill missing seconds (no-trade seconds are absent rows)
    full = pd.RangeIndex(day0, day0 + 86400, name="sec").to_frame(index=False)
    df = full.merge(df[["sec", "close"]], on="sec", how="left")
    df["close"] = df["close"].ffill().bfill()
    return df


def time_gmtime(day0: int) -> str:
    import time as _t
    return _t.strftime("%Y-%m-%d", _t.gmtime(day0))


def twap60(close: np.ndarray) -> np.ndarray:
    """Rolling mean of current+previous 59 closes."""
    s = pd.Series(close)
    return s.rolling(60, min_periods=30).mean().to_numpy()


def main():
    rows = []
    label_stats = {"match": 0, "total": 0}
    for gf in sorted(GAMMA.glob("*_15m.jsonl")):
        sym_slug = gf.stem.split("_")[0]              # btc / eth / sol
        symbol = sym_slug.upper() + "USDT"
        markets = []
        with open(gf) as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    if not rec.get("MISS") and rec.get("outcomePrices"):
                        markets.append(rec)
                except Exception:
                    pass
        by_day = {}
        for m in markets:
            day0 = (m["ts"] // 86400) * 86400
            by_day.setdefault(day0, []).append(m)

        kcache = {}
        for day0, mkts in sorted(by_day.items()):
            if day0 not in kcache:
                kcache = {day0: load_kline_day(symbol, day0)}
            kl = kcache[day0]
            if kl is None:
                continue
            sec = kl["sec"].to_numpy()
            close = kl["close"].to_numpy()
            tw = twap60(close)
            base_idx = day0
            for m in mkts:
                ts = m["ts"]
                i0 = ts - base_idx
                i_end = i0 + 900                      # exclusive
                if i0 < 61 or i_end + 1 > len(close):
                    continue
                w_close = close[i0:i_end]
                w_tw = tw[i0:i_end]
                strike = float(w_tw[59])              # TWAP at second 59 (60th second)
                final_tw = float(np.nanmean(w_close[-60:]))
                recon_up = final_tw >= strike
                try:
                    prices = m["outcomePrices"]
                    if isinstance(prices, str):
                        prices = json.loads(prices)
                    outs = m["outcomes"]
                    if isinstance(outs, str):
                        outs = json.loads(outs)
                    actual_up = prices[outs.index("Up")] == "1"
                except Exception:
                    continue
                label_stats["total"] += 1
                ok = bool(recon_up) == bool(actual_up)
                label_stats["match"] += int(ok)
                winner_up = bool(actual_up)

                # pre-window context
                pre_ret_1h = (close[i0 - 1] / close[i0 - 3600] - 1) * 1e4
                pre_vol = float(np.std(np.diff(np.log(close[i0 - 600:i0]))) * 1e4)

                r = np.diff(np.log(w_close))
                crosses = 0
                above_prev = w_tw[59] >= strike
                for T in DECISION_TIMES:
                    if T > 870:
                        break
                    it = T                            # index of second T within window
                    d = (w_tw[it] - strike) / strike * 1e4
                    above = w_tw[it] >= strike
                    if above != above_prev:
                        crosses += 1
                        above_prev = above
                    vol60 = float(np.std(r[max(0, it - 60):it]) * 1e4) if it >= 10 else np.nan
                    vol300 = float(np.std(r[max(0, it - 300):it]) * 1e4) if it >= 60 else np.nan
                    max_exc = float(np.max(np.abs((w_tw[:it + 1] - strike) / strike))) * 1e4
                    leader_up = bool(d >= 0)
                    rows.append({
                        "symbol": sym_slug, "ts": ts, "T": T,
                        "hour": int(time.gmtime(ts)[11:13]) if False else ((ts % 86400) // 3600),
                        "dist_bps": d,
                        "leader": "UP" if leader_up else "DOWN",
                        "leader_won": 1 if leader_up == winner_up else 0,
                        "vol60_bps": vol60, "vol300_bps": vol300,
                        "max_exc_bps": max_exc, "crosses": crosses,
                        "pre_ret1h_bps": pre_ret_1h, "pre_vol_bps": pre_vol,
                        "winner": "UP" if winner_up else "DOWN",
                        "label_match": int(ok),
                    })
        print(f"{sym_slug}: processed {len(by_day)} days", flush=True)

    df = pd.DataFrame(rows)
    out = ROOT / "data" / "training_dataset.parquet"
    df.to_parquet(out, index=False)
    print(f"\nrows={len(df)}  markets={df.ts.nunique() if len(df) else 0}")
    print(f"label reconstruction vs Chainlink resolution: "
          f"{label_stats['match']}/{label_stats['total']} = "
          f"{100*label_stats['match']/max(1,label_stats['total']):.2f}%")
    print("saved ->", out)


if __name__ == "__main__":
    main()
