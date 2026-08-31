"""Step 1b: corrected checks (runs test w/o overflow, OHLC sanity, significance of ACs)."""
import sys
sys.path.insert(0, "/home/alex/Project/up_down/research")
import numpy as np
from scipy import stats
from common import load_data

df = load_data()
bad = ((df.high < df[["open", "close", "low"]].max(axis=1)) |
       (df.low > df[["open", "close"]].min(axis=1))).sum()
print(f"OHLC sanity violations: {int(bad)}")
print(f"non-positive volumes: {int((df.volume <= 0).sum())}")

f_dir = np.sign(df.close - df.open).replace(0, 1).to_numpy()
y = f_dir[1:]  # target = next direction
n = len(y)
runs = 1 + int((y[1:] != y[:-1]).sum())
n_up = int((y == 1).sum()); n_dn = n - n_up
mu = 2.0 * n_up * n_dn / n + 1
var = (2.0 * n_up * n_dn * (2.0 * n_up * n_dn - n)) / (float(n) * n * (n - 1))
z = (runs - mu) / np.sqrt(var)
pv = 2 * stats.norm.sf(abs(z))
print(f"\nRuns test: runs={runs}, expected={mu:.0f}, sd={np.sqrt(var):.0f}, "
      f"z={z:.3f}, p={pv:.3e}")
print("  -> more runs than random = slight anti-persistence (mean reversion)")

# exact binomial for the strongest simple conditional
wins = int(((y == 1) & (np.r_[f_dir[:-1]] == -1)).sum())
nn = int((np.r_[f_dir[:-1]] == -1).sum())
p = stats.binomtest(wins, nn, 0.5, alternative="greater")
print(f"\nP(up | prev DOWN) = {wins}/{nn} = {wins/nn:.4f}, binomial p(one-sided)={p.pvalue:.2e}")

# per-year stability of the flip edge
import pandas as pd
t = df.open_time.iloc[1:].reset_index(drop=True)
yr = t.dt.year.to_numpy()
for yv in sorted(set(yr)):
    m = yr == yv
    w = wins if False else None
    prev_dn = np.r_[f_dir[:-1]] == -1
    mm = m & prev_dn
    ww = int(((y == 1) & mm).sum()); cnt = int(mm.sum())
    print(f"  year {yv}: P(up|prev DN) = {ww/cnt:.4f} (n={cnt})")
