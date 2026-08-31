"""Fill-simulation backtest: LUT fair value vs real Polymarket trade prints.

Entry rule: at decision time T, if fair_value(leader) - MARGIN >= best observed
ask-level (BUY prints = executions AT asks), assume taker fill at that price.
PnL net of taker fee (baseRate 1000bps x min(p,1-p), crypto_fees_v2).

Caveats (documented): prints are executions, not resting depth; our own order
could move price / queue effects ignored -> treat results as optimistic bound;
conservative margins required.
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/alex/Project/up_down/research")
from common import wilson_ci  # noqa: E402

ROOT = Path("/home/alex/Project/up_down")
OUT = ROOT / "data" / "trades"
TAKER_BASE_RATE = 0.10          # takerBaseFee=1000 bps
FEE_FORMULA = lambda p: TAKER_BASE_RATE * min(p, 1 - p)


def load_lut():
    lut = pd.read_csv(ROOT / "data" / "lut_fair_value.csv")
    lut["abs_bin_lo"] = lut["abs_bin"].str.replace("(", "").str.replace(",", " ").str.split().str[0].astype(float)
    return lut


def fair_for(lut, t_bucket, abs_dist):
    sub = lut[lut.t_bucket == t_bucket]
    idx = np.searchsorted(sub.abs_bin_lo.values, abs_dist, side="right") - 1
    if idx < 0:
        return np.nan
    return float(sub.fair_value.iloc[idx])


def t_bucket_for(T):
    if T <= 240:
        return "T<=240"
    if T <= 480:
        return "240<T<=480"
    if T <= 720:
        return "480<T<=720"
    return "T>720"


def main(margin=0.03, min_size=5):
    ds = pd.read_parquet(ROOT / "data" / "training_dataset.parquet")
    ds["day"] = ds["ts"] // 86400
    days = sorted(ds.day.unique())
    cut_day = days[int(len(days) * 0.7)]
    test = ds[ds.day >= cut_day].copy()
    lut = load_lut()

    results = []
    trade_files = sorted(OUT.glob("shard_*of*.parquet")) + \
        [OUT / f"{s}_15m.parquet" for s in ("btc", "eth", "sol") if (OUT / f"{s}_15m.parquet").exists()]
    trade_files += list(OUT.glob("shard_*.parquet.tmp"))
    pr_all = pd.concat([pd.read_parquet(f) for f in trade_files if f.exists()], ignore_index=True)
    print(f"loaded {len(pr_all)} prints from {len(trade_files)} files")
    # index prints by (symbol, mkt_ts) once
    pr_all["outcome"] = pr_all["outcome"].str.upper()
    pr_all = pr_all[pr_all.outcome.isin(["UP", "DOWN"])]
    pr_groups = {k: g for k, g in pr_all.groupby(["symbol", "mkt_ts"])}
    for sym, sym_df in test.groupby("symbol"):
        for r in sym_df.itertuples():
            pr = pr_groups.get((sym, r.ts))
            if pr is None or not len(pr):
                continue
            fair = fair_for(lut, t_bucket_for(r.T), abs(r.dist_bps))
            if np.isnan(fair):
                continue
            thr = fair - margin
            tt = pr.t.to_numpy()
            mask = (tt >= r.ts + r.T - 5) & (tt <= r.ts + r.T + 25)
            if not mask.any():
                continue
            w = pr[mask]
            lead_pr = w[w.outcome == r.leader]
            buys = lead_pr[(lead_pr.side == "BUY") & (lead_pr.price <= thr)]
            if not len(buys):
                continue
            buys = buys.sort_values("t")
            if buys["size"].max() < min_size and buys["size"].sum() < min_size * 2:
                continue
            px = float(buys.iloc[0].price)
            fee = FEE_FORMULA(px)
            pnl = (1 - px - fee) if r.leader_won == 1 else (-px - fee)
            results.append({
                "symbol": sym, "ts": r.ts, "day": r.day, "T": int(r.T),
                "dist_bps": r.dist_bps, "fair": fair, "entry": px,
                "won": int(r.leader_won), "pnl": pnl,
                "t_bucket": t_bucket_for(int(r.T)),
            })

    res = pd.DataFrame(results)
    if not len(res):
        print("no fills simulated — check trades data coverage")
        return
    res.to_parquet(ROOT / "data" / "backtest_fills.parquet", index=False)

    print(f"=== FILL-SIM BACKTEST (margin={margin}, test days only) ===")
    n = len(res)
    wr = res.won.mean()
    lo, hi = wilson_ci(int(res.won.sum()), n)
    print(f"fills={n} | WR={wr:.3f} [{lo:.3f},{hi:.3f}] | avg entry={res.entry.mean():.3f} "
          f"| avg fair={res.fair.mean():.3f}")
    ev_per = res.pnl.mean()
    print(f"EV per filled trade: {ev_per:+.4f} $/share | total PnL @1 share: {res.pnl.sum():+.2f}")
    print(f"\nby T bucket:")
    for tb, g in res.groupby("t_bucket"):
        l2, h2 = wilson_ci(int(g.won.sum()), len(g))
        print(f"  {tb:>12}: n={len(g):>4} WR={g.won.mean():.3f} [{l2:.3f},{h2:.3f}] "
              f"entry={g.entry.mean():.3f} EV={g.pnl.mean():+.4f}")
    print(f"\nby day:")
    for d, g in res.sort_values('day').groupby("day"):
        print(f"  {d}: n={len(g):>3} WR={g.won.mean():.3f} EV={g.pnl.mean():+.4f} PnL={g.pnl.sum():+.2f}")


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 0.03)
