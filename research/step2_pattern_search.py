"""Step 2: exhaustive pattern search over discrete feature space.
Every pattern = a cell of a discretized feature combo. We record n, WR=P(next up),
binomial p vs 0.5, BH q-value. Selection mirrors a naive researcher:
'max WR subject to n >= min_n'.
"""
import sys
sys.path.insert(0, "/home/alex/Project/up_down/research")
import numpy as np
import pandas as pd
from scipy import stats
from common import bh_qvalues, wilson_ci
import json

f = pd.read_parquet("/home/alex/Project/up_down/research/features.parquet")
y = (f["y"] == 1).to_numpy().astype(np.int64)   # 1 = next candle up
n_total = len(y)

def codes(*series_list):
    """Combine discrete series into a single non-negative integer code array."""
    out = np.zeros(n_total, dtype=np.int64)
    mult = 1
    for s in series_list:
        arr = np.asarray(s)
        arr = arr - arr.min()          # shift to non-negative
        out += arr * mult
        mult *= int(arr.max()) + 1
    return out

dir_ = f["dir"].to_numpy()
# ---- Family definitions -------------------------------------------------
fam = {}

# F1: direction history of last k candles (as string U/D)
for k in range(1, 7):
    hist = np.zeros(n_total, dtype=np.int64)
    for j in range(k):
        hist = hist * 2 + ((f["dir"].shift(j).fillna(1).to_numpy() == 1).astype(np.int64))
    fam[f"F1_hist{k}"] = hist

st = f["streak"].to_numpy()
fam["F2_streak"] = pd.Series(np.sign(st) * np.minimum(np.abs(st), 8))          # -8..+8
fam["F3_rsi"] = pd.Series(np.digitize(f["rsi"], [10, 20, 30, 40, 60, 70, 80, 90]))
fam["F4_shape"] = pd.Series(
    np.digitize(f["body_ratio"], [1/3, 2/3]) * 4 +
    np.digitize(f["close_pos"], [0.25, 0.75]))                                  # 12 cells
fam["F5_vol"] = pd.Series(np.digitize(f["vol_z"], [-1.0, 0.0, 1.0]))
fam["F6_hour"] = f["hour"]
fam["F7_band"] = pd.Series(np.digitize(f["band_pos"], [0.1, 0.3, 0.7, 0.9]))
fam["F8_wick"] = pd.Series(
    (f["upper_wick"] > f["lower_wick"]).astype(int) * 2 +
    (np.maximum(f["upper_wick"], f["lower_wick"]) > 0.3).astype(int))
fam["F9_hist2xRSI"] = codes((pd.Series(np.digitize(f["rsi"], [35, 65]))),
                            pd.Series(dir_ * 2 + (f["dir"].shift(1).fillna(1).to_numpy() == 1).astype(int)))
fam["F10_hist2xBAND"] = codes(pd.Series(np.digitize(f["band_pos"], [0.25, 0.75])),
                              pd.Series(dir_ * 2 + (f["dir"].shift(1).fillna(1).to_numpy() == 1).astype(int)))
fam["F11_streakxHOURBLK"] = codes(pd.Series(np.sign(st)), pd.Series(f["hour"] // 4))
fam["F12_dirxVOL"] = codes(pd.Series(dir_), fam["F5_vol"])
h3 = np.zeros(n_total, dtype=np.int64)
for j in range(3):
    h3 = h3 * 2 + ((f["dir"].shift(j).fillna(1).to_numpy() == 1).astype(np.int64))
fam["F13_hist3xBAND"] = codes(pd.Series(np.digitize(f["band_pos"], [0.25, 0.75])), pd.Series(h3))

rows = []
for name, cser in fam.items():
    c = cser.to_numpy() if hasattr(cser, "to_numpy") else np.asarray(cser)
    c = (c - c.min()).astype(np.int64)  # normalize to non-negative codes
    uniq = np.unique(c)
    tot = np.bincount(c)[uniq]
    ups = np.bincount(c, weights=y)[uniq]
    for u, t_, w_ in zip(uniq, tot, ups):
        t_i, w_i = int(t_), int(w_)
        if t_i < 10:
            continue
        wr = w_i / t_i
        # two-sided binomial vs fair coin
        pv = stats.binomtest(w_i, t_i, 0.5).pvalue
        lo, hi = wilson_ci(w_i, t_i)
        rows.append(dict(family=name, cell=int(u), n=t_i, wins_up=w_i,
                         wr_up=wr, p=pv, wilson_lo=lo, wilson_hi=hi))

res = pd.DataFrame(rows)
res["p_side"] = res.apply(lambda r: stats.binomtest(max(r.wins_up, r.n - r.wins_up),
                                                    r.n, 0.5, alternative="greater").pvalue, axis=1)
res["q_bh"] = bh_qvalues(res["p_side"].to_numpy())
res.to_csv("/home/alex/Project/up_down/research/pattern_results.csv", index=False)

M = len(res)
print(f"=== PATTERN SEARCH ===")
print(f"families: {len(fam)}, total patterns tested (n>=10): {M}")

sig = res[res.q_bh < 0.05]
print(f"\npatterns significant after BH q<0.05: {len(sig)} ({100*len(sig)/M:.1f}% of all)")

print("\n--- Best WR per family (n>=100) ---")
top = res[res.n >= 100].sort_values("wr_up", ascending=False)
shown_fams = set()
for _, r in top.iterrows():
    if r.family in shown_fams:
        continue
    shown_fams.add(r.family)
    print(f"  {r.family:22s} cell={r.cell:>3} n={r.n:>6} P(up)={r.wr_up:.4f} "
          f"[{r.wilson_lo:.3f},{r.wilson_hi:.3f}] q={r.q_bh:.2e}")
    if len(shown_fams) >= 13:
        break

print("\n--- Naive selection: max |WR-0.5| for various min_n ---")
summary = {}
for mn in [20, 50, 100, 300, 1000]:
    sub = res[(res.n >= mn) & (res.q_bh.notna())]
    best = sub.loc[sub.p_side.idxmin()]
    summary[mn] = dict(n=int(best.n), wr=float(best.wr_up), family=best.family,
                       cell=int(best.cell), p=float(best.p_side), q=float(best.q_bh))
    print(f"  min_n={mn:>4}: best={best.family}/cell{int(best.cell)} n={int(best.n)} "
          f"WR={best.wr_up:.4f} raw_p={best.p_side:.2e} q_BH={best.q_bh:.2e}")

json.dump(summary, open("/home/alex/Project/up_down/research/step2_summary.json", "w"), indent=1)

print("\n--- How many 'perfect' (100% WR) patterns exist? ---")
for mn in [10, 20, 50, 100]:
    cnt = int(((res.n >= mn) & ((res.wr_up == 1.0) | (res.wr_up == 0.0))).sum())
    print(f"  n>={mn}: {cnt} perfect patterns out of {int((res.n>=mn).sum())} tested")
