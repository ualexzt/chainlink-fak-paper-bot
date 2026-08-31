"""Backtest v2: per-print FRESH fair value (kills stale-quote adverse selection).

For each candidate BUY print of the leader side at time tau:
  - recompute dist_bps from Binance TWAP60 exactly at tau
  - require sign matches leader and |dist| >= floor (basis-noise guard)
  - fresh_fair = shrunk LUT[(t_bucket(tau-ts), |dist| bin)]
  - fill only if print price <= fresh_fair - margin
PnL net of taker fee.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/alex/Project/up_down/research")
from common import wilson_ci  # noqa: E402

ROOT = Path("/home/alex/Project/up_down")
OUT = ROOT / "data" / "trades"
KLINES = ROOT / "data" / "binance"
TAKER_BASE_RATE = 0.10


def fee(p):
    return TAKER_BASE_RATE * min(p, 1 - p)


def load_lut():
    return pd.read_csv(ROOT / "data" / "lut_fair_value.csv")


def make_lut_lookup(lut):
    buckets = {}
    for tb, g in lut.groupby("t_bucket"):
        g = g.sort_values("abs_bin").reset_index(drop=True)
        los = g["abs_bin"].str.replace("(", "").str.replace(",", " ").str.split().str[0].astype(float).values
        buckets[tb] = (los, g["fair_value"].values)
    def lookup(tb, abs_dist):
        b = buckets.get(tb)
        if b is None:
            return np.nan
        los, fv = b
        i = np.searchsorted(los, abs_dist, side="right") - 1
        return float(fv[i]) if i >= 0 else np.nan
    return lookup


def t_bucket_for(T):
    if T <= 240:
        return "T<=240"
    if T <= 480:
        return "240<T<=480"
    if T <= 720:
        return "480<T<=720"
    return "T>720"


class PriceCache:
    """Per (symbol, day): close[], twap60[], base_day0."""
    def __init__(self):
        self.cache = {}

    def get(self, symbol, day0):
        key = (symbol, day0)
        if key not in self.cache:
            import time as _t
            f = KLINES / f"{symbol.upper()}USDT" / f"{_t.strftime('%Y-%m-%d', _t.gmtime(day0))}.parquet"
            if not f.exists():
                self.cache[key] = None
                return None
            df = pd.read_parquet(f)
            df["close"] = df["close"].astype(float)
            df["sec"] = df["open_time"] // 1000
            full = pd.RangeIndex(day0, day0 + 86400, name="sec").to_frame(index=False)
            df = full.merge(df[["sec", "close"]], on="sec", how="left")
            df["close"] = df["close"].ffill().bfill()
            c = df["close"].to_numpy()
            tw = pd.Series(c).rolling(60, min_periods=30).mean().to_numpy()
            self.cache[key] = (day0, c, tw)
        return self.cache[key]

    def dist_at(self, symbol, tau, strike):
        day0 = (tau // 86400) * 86400
        arr = self.get(symbol, day0)
        if arr is None:
            return np.nan
        _, _, tw = arr
        i = tau - day0
        if i < 59 or i >= len(tw) or not np.isfinite(strike) or strike <= 0:
            return np.nan
        return (tw[i] - strike) / strike * 1e4

    def strike_for(self, symbol, ts):
        arr = self.get(symbol, (ts // 86400) * 86400)
        if arr is None:
            return np.nan
        day0, _, tw = arr
        i = ts - day0 + 59
        if i >= len(tw):
            return np.nan
        return float(tw[i])


def main(margin=0.04, dist_floor=3.0, max_lag=5, only_tmin=0, only_tmax=100000,
         symbols=("btc", "eth", "sol")):
    ds = pd.read_parquet(ROOT / "data" / "training_dataset.parquet")
    ds["day"] = ds["ts"] // 86400
    days = sorted(ds.day.unique())
    cut_day = days[int(len(days) * 0.7)]
    test = ds[(ds.day >= cut_day)].copy()
    lut = load_lut()
    lookup = make_lut_lookup(lut)
    pc = PriceCache()

    files = sorted(OUT.glob("shard_*of4.parquet.tmp")) + sorted(OUT.glob("shard_*of4.parquet"))
    pr_all = pd.concat([pd.read_parquet(f) for f in files if f.exists()], ignore_index=True)
    pr_all["outcome"] = pr_all["outcome"].astype(str).str.upper()
    pr_all = pr_all[pr_all.outcome.isin(["UP", "DOWN"]) & (pr_all.side == "BUY")]
    print(f"prints loaded: {len(pr_all)}")

    # group test rows per market
    mkt_rows = {(r.symbol, r.ts): r for r in test.itertuples()}
    pr_by_key = {k: g.sort_values("t") for k, g in pr_all.groupby(["symbol", "mkt_ts"])}

    results = []
    n_markets_with_prints = 0
    for (sym, mts), pr in pr_by_key.items():
        r = mkt_rows.get((sym, mts))
        if r is None:
            continue
        n_markets_with_prints += 1
        strike = pc.strike_for(sym, mts)
        if not np.isfinite(strike):
            continue
        tt = pr.t.to_numpy()
        lo, hi = mts + r.T - 2, mts + r.T + max_lag
        wmask = (tt >= lo) & (tt <= hi)
        if not wmask.any():
            continue
        w = pr[wmask]
        lead_pr = w[w.outcome == r.leader]
        if not len(lead_pr):
            continue
        for cand in lead_pr.itertuples():
            tau = int(cand.t)
            age = tau - (mts + r.T)          # seconds after decision moment
            if age < -2:
                continue
            d_now = pc.dist_at(sym, tau, strike)
            if not np.isfinite(d_now) or abs(d_now) < dist_floor:
                continue
            if (d_now >= 0) != (r.leader == "UP"):
                continue                     # leader no longer leading -> skip
            fv = lookup(t_bucket_for(age + r.T if False else r.T), abs(d_now))
            if np.isnan(fv):
                continue
            thr = fv - margin
            if cand.price > thr:
                continue
            px = float(cand.price)
            pnl = (1 - px - fee(px)) if r.leader_won == 1 else (-px - fee(px))
            results.append({
                "symbol": sym, "ts": mts, "day": r.day, "T": int(r.T),
                "age": age, "entry": px, "fair": fv, "fresh_dist": d_now,
                "won": int(r.leader_won), "pnl": pnl,
                "t_bucket": t_bucket_for(int(r.T)),
            })
            break                            # one fill per decision point

    res = pd.DataFrame(results)
    print(f"\nmarkets with prints: {n_markets_with_prints} | decision points filled: {len(res)}")
    if not len(res):
        print("no fills")
        return
    res.to_parquet(ROOT / "data" / f"backtest_v2_m{margin}_f{dist_floor}.parquet", index=False)
    n = len(res)
    lo_, hi_ = wilson_ci(int(res.won.sum()), n)
    print(f"\n=== V2 BACKTEST margin={margin} floor={dist_floor}bps lag<={max_lag}s ===")
    print(f"fills={n} WR={res.won.mean():.3f} [{lo_:.3f},{hi_:.3f}] "
          f"avg_entry={res.entry.mean():.3f} avg_fair={res['fair'].mean():.3f}")
    print(f"EV/trade={res.pnl.mean():+.4f} $/share | PnL total={res.pnl.sum():+.1f} @1share")
    print("\nby T bucket:")
    for tb, g in res.groupby("t_bucket"):
        l2, h2 = wilson_ci(int(g.won.sum()), len(g))
        print(f"  {tb:>12}: n={len(g):>5} WR={g.won.mean():.3f} [{l2:.3f},{h2:.3f}] "
              f"EV={g.pnl.mean():+.4f}")
    print("\nby day:")
    for d, g in res.groupby("day"):
        print(f"  {d}: n={len(g):>4} WR={g.won.mean():.3f} EV={g.pnl.mean():+.4f} PnL={g.pnl.sum():+.2f}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--margin", type=float, default=0.04)
    ap.add_argument("--floor", type=float, default=3.0)
    ap.add_argument("--lag", type=int, default=5)
    a = ap.parse_args()
    main(a.margin, a.floor, a.lag)
