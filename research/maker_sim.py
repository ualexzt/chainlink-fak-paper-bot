"""Maker upper-bound simulation (chronological stateful replay).

Honest mechanics:
 - We hypothetically rest ONE order (variant S: leader-side bid only;
   variant D: both sides) priced at fresh_fair - delta, re-quoted every second
   from the same LUT + Binance TWAP used in backtest_v2.
 - A SELL print of outcome X at price p fills our resting bid on X iff our
   current level B >= p  ->  we buy at B (limit executes at our price).
 - One open position at a time; held to window resolution (win: +1-B,
   lose: -B). Maker fee = 0. Rebates reported separately (formula TBD).
 - OPTIMISTIC assumptions (this is an UPPER BOUND on maker EV):
     * we see/cancel instantly at each new second (no latency)
     * queue position ignored: any crossing sell takes us out fully
     * our own resting liquidity did not alter the tape

Usage: .venv/bin/python research/maker_sim.py [delta] [variant S|D]
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/alex/Project/up_down/research")
from common import wilson_ci          # noqa: E402
from backtest_v2 import (PriceCache, load_lut, make_lut_lookup, t_bucket_for)  # noqa: E402

ROOT = Path("/home/alex/Project/up_down")
OUT = ROOT / "data" / "trades"


def run(delta: float, variant: str = "S", dist_floor: float = 3.0):
    ds = pd.read_parquet(ROOT / "data" / "training_dataset.parquet",
                         columns=["symbol", "ts", "winner"])
    ds["day"] = ds["ts"] // 86400
    days = sorted(ds.day.unique())
    cut_day = days[int(len(days) * 0.7)]
    meta = ds[ds.day >= cut_day].drop_duplicates(["symbol", "ts"])

    lut = load_lut()
    lookup = make_lut_lookup(lut)
    pc = PriceCache()

    files = sorted(OUT.glob("shard_*of4.parquet")) + sorted(OUT.glob("oldperiod_shard*.parquet"))
    pr = pd.concat([pd.read_parquet(f) for f in files if f.exists()], ignore_index=True)
    pr["outcome"] = pr.outcome.astype(str).str.upper()
    sells = pr[(pr.side == "SELL") & pr.outcome.isin(["UP", "DOWN"])]
    # test-period markets only
    sells = sells[sells.mkt_ts >= cut_day * 86400]
    sells = sells.sort_values(["symbol", "mkt_ts", "t"])
    print(f"sell prints (test period): {len(sells)} "
          f"across {sells.groupby(['symbol','mkt_ts']).ngroups} markets")

    meta_map = {(r.symbol, r.ts): r.winner for r in meta.itertuples()}

    results = []
    n_markets_quoted = 0
    for (sym, mts), g in sells.groupby(["symbol", "mkt_ts"], sort=True):
        winner = meta_map.get((sym, mts))
        if winner is None:
            continue
        strike = pc.strike_for(sym, mts)
        if not np.isfinite(strike):
            continue
        end_ts = mts + 900
        n_markets_quoted += 1

        open_pos = None            # dict(side, entry, tau)
        cache_sec = None
        cur_bids = {}              # outcome -> bid level this second
        for row in g.itertuples():
            t = int(row.t)
            if t >= end_ts - 5:
                break
            if open_pos is not None:
                continue           # already holding; ride to settlement
            if cache_sec != t:
                cache_sec = t
                cur_bids = {}
                age = t - mts
                if age < 65:
                    continue
                d = pc.dist_at(sym, t, strike)
                if np.isfinite(d) and abs(d) >= dist_floor:
                    leader = "UP" if d >= 0 else "DOWN"
                    fv_l = lookup(t_bucket_for(age), abs(d))
                    if np.isfinite(fv_l):
                        cur_bids[leader] = fv_l - delta
                        if variant == "D":
                            cur_bids["DOWN" if leader == "UP" else "UP"] = \
                                (1.0 - fv_l) - delta
            o, p = row.outcome, float(row.price)
            b = cur_bids.get(o)
            if b is not None and b >= p:
                open_pos = {"side": o, "entry": b, "tau": t}
                won = 1 if o == winner else 0
                pnl = (1.0 - b) if won else (-b)
                results.append({
                    "symbol": sym, "ts": mts, "day": mts // 86400,
                    "tau_age": t - mts, "side": o, "entry": b,
                    "won": won, "pnl": pnl,
                })
                cache_sec = None       # force re-quote next event

    res = pd.DataFrame(results)
    print(f"\n=== MAKER UPPER-BOUND SIM delta={delta} variant={variant} ===")
    print(f"markets quoted: {n_markets_quoted} | fills: {len(res)}")
    if not len(res):
        return
    lo, hi = wilson_ci(int(res.won.sum()), len(res))
    ev = res.pnl.mean()
    print(f"WR={res.won.mean():.3f} [{lo:.3f},{hi:.3f}] | avg entry={res.entry.mean():.3f} "
          f"| EV={ev:+.4f} $/share | PnL={res.pnl.sum():+.1f}")
    matched_usd = res.entry.sum()
    print(f"matched notional: ${matched_usd:,.0f} over {res.day.nunique()} days "
          f"(~${matched_usd/res.day.nunique():,.0f}/day)")
    print("\nby side:")
    for s, gg in res.groupby("side"):
        l2, h2 = wilson_ci(int(gg.won.sum()), len(gg))
        print(f"  {s:>4}: n={len(gg):>4} WR={gg.won.mean():.3f} [{l2:.3f},{h2:.3f}] "
              f"avg_entry={gg.entry.mean():.3f} EV={gg.pnl.mean():+.4f}")
    print("\nby fill age:")
    res["age_bucket"] = pd.cut(res.tau_age, [60, 240, 480, 720, 900],
                               labels=["<=240", "240-480", "480-720", ">720"])
    for ab, gg in res.groupby("age_bucket", observed=True):
        print(f"  {str(ab):>8}: n={len(gg):>4} WR={gg.won.mean():.3f} EV={gg.pnl.mean():+.4f}")
    print("\nby day:")
    for d, gg in res.groupby("day"):
        print(f"  {d}: n={len(gg):>3} WR={gg.won.mean():.3f} EV={gg.pnl.mean():+.4f} "
              f"PnL={gg.pnl.sum():+.2f}")
    res.to_parquet(ROOT / "data" / f"maker_sim_{variant}_d{delta}.parquet", index=False)


if __name__ == "__main__":
    d = float(sys.argv[1]) if len(sys.argv) > 1 else 0.03
    v = sys.argv[2] if len(sys.argv) > 2 else "S"
    run(d, v)
