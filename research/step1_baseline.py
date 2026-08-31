"""Step 1: data quality + baseline randomness statistics."""
import sys
sys.path.insert(0, "/home/alex/Project/up_down/research")
import numpy as np
from scipy import stats
from common import load_data, data_quality, build_features

df = load_data()
q = data_quality(df)
print("=== DATA QUALITY ===")
for k, val in q.items():
    print(f"  {k}: {val}")

f = build_features(df)
y = f["y"].to_numpy()
n = len(y)
p_up = (y == 1).mean()
print(f"\n=== BASELINE (n={n}) ===")
print(f"P(next candle UP) = {p_up:.4f}  ({(y==1).sum()} up / {(y==-1).sum()} down)")
se = np.sqrt(p_up * (1 - p_up) / n)
print(f"  95% CI for P(up): [{p_up-1.96*se:.4f}, {p_up+1.96*se:.4f}]")

# Runs test on direction sequence
runs = 1 + int((y[1:] != y[:-1]).sum())
n_up = (y == 1).sum(); n_dn = n - n_up
mu = 2 * n_up * n_dn / n + 1
var = 2 * n_up * n_dn * (2 * n_up * n_dn - n) / (n * n * (n - 1))
z = (runs - mu) / np.sqrt(var)
pv = 2 * stats.norm.sf(abs(z))
print(f"\nRuns test: observed runs={runs}, expected={mu:.0f}, z={z:.3f}, p={pv:.4f}")

# Autocorrelation of directions and returns
print("\n=== AUTOCORRELATION ===")
yd = y - y.mean()
ac_y = np.correlate(yd, yd, "full")[len(yd)-1:] / (np.arange(len(yd), 0, -1) * yd.var())
r = f["ret"].to_numpy()
rd = r - r.mean()
ac_r = np.correlate(rd, rd, "full")[len(rd)-1:] / (np.arange(len(rd), 0, -1) * rd.var())
band = 1.96 / np.sqrt(n)
lags = [1, 2, 3, 5, 10, 20, 50, 100, 288]
print(f"  95% noise band: ±{band:.5f}")
print(f"{'lag':>4} {'AC(dir)':>12} {'AC(ret)':>12}")
for L in lags:
    print(f"{L:>4} {ac_y[L]:>12.5f} {ac_r[L]:>12.5f}")

# Ljung-Box on returns at lag 20
lb = n * (n + 2) * np.sum(ac_r[1:21] ** 2 / (np.arange(1, 21)))
print(f"\nLjung-Box(20) on returns: Q={lb:.1f}, p={stats.chi2.sf(lb, 20):.2e}")

# Conditional probabilities after simple events
print("\n=== SIMPLE CONDITIONALS ===")
for name, mask in [
    ("prev UP",            f["dir"].to_numpy() == 1),
    ("prev DOWN",          f["dir"].to_numpy() == -1),
    ("streak >= +3 ups",   f["streak"].to_numpy() >= 3),
    ("streak <= -3 downs", f["streak"].to_numpy() <= -3),
    ("big prev candle (body>0.8)", f["body_ratio"].to_numpy() > 0.8),
]:
    nn = mask.sum(); wins = (y[mask] == 1).sum()
    print(f"  {name:32s} n={nn:>6}  P(up)={wins/nn:.4f}")

# Distribution of next-return magnitude (what does 'predicting the candle' even buy us)
nr = f["next_ret"].to_numpy()
print(f"\nnext |return| percentiles: 50%={np.percentile(np.abs(nr),50)*1e4:.1f}bp, "
      f"90%={np.percentile(np.abs(nr),90)*1e4:.1f}bp, 99%={np.percentile(np.abs(nr),99)*1e4:.1f}bp")

# Save features once for later steps
f.to_parquet("/home/alex/Project/up_down/research/features.parquet")
print("\nfeatures saved -> research/features.parquet")
