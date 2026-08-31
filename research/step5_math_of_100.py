"""Step 5: the mathematics of '100% win rate' claims.

Answers three questions:
A) Given OUR 264-pattern search, how many 'perfect' patterns would pure luck
   manufacture? (analytic expectation + MC confirmation from step3)
B) If a TRUE 99%-WR strategy existed, how often would it LOOK 100% on small
   samples? (i.e., what does a small-sample 100% streak actually prove?)
C) Minimum verified sample size to distinguish true WR 99% (or 60%) from a
   fair coin at standard significance/power.
"""
import sys, json
sys.path.insert(0, "/home/alex/Project/up_down/research")
import numpy as np
import pandas as pd
from scipy import stats

res = pd.read_csv("/home/alex/Project/up_down/research/pattern_results.csv")

print("=== A) Expected number of PERFECT patterns from pure luck ===")
for mn in [5, 10, 20, 50]:
    sub = res[res.n >= mn]
    exp_flukes = float((2.0 ** (1 - sub.n)).sum())   # E[#cells all-same-direction] under H0
    print(f"  n>={mn}: {len(sub)} patterns, expected lucky-perfect count = {exp_flukes:.4f}")

print("\n=== B) What does an observed 100% streak prove? ===")
# If true WR were q, probability of seeing zero losses in N trades:
for q in [0.99, 0.90, 0.80, 0.70, 0.60]:
    probs = [q ** N for N in (8, 15, 26, 50)]
    print(f"  true WR={q:.0%}: P(100% visible over N trades) -> "
          + ", ".join(f"N={N}: {p:.0%}" for N, p in zip((8, 15, 26, 50), probs)))

print("\n=== C) Sample size needed to certify a claimed WR ===")
def min_n_reject_coin(alpha=0.05):
    """All-wins streak long enough to reject a fair coin (one-sided)."""
    n = 1
    while 0.5 ** n > alpha:
        n += 1
    return n

print(f"  reject 'pure luck': N={min_n_reject_coin()} consecutive wins "
      f"(alpha=0.05) -- but this proves only WR>50%, not the claimed level")

def n_allwins_for_lcl(target_wr, z=1.96):
    """Consecutive ALL-WIN trades needed so Wilson 95% LOWER bound >= target_wr."""
    n = target_wr
    while True:
        p = 1.0
        denom = 1 + z * z / n
        centre = p + z * z / (2 * n)
        half = z * np.sqrt(0 / n + z * z / (4 * n * n))  # q_hat = 0 when all wins
        lcl = (centre - half) / denom if False else 1.0 / (1 + z * z / n)
        if lcl >= target_wr:
            return int(n)
        n += 1
        if n > 10_000_000:
            return None

for lvl in [0.90, 0.99, 0.999]:
    print(f"  certify 'true WR >= {lvl*100:g}%': needs N={n_allwins_for_lcl(lvl):,} "
          f"VERIFIED consecutive wins (Wilson LCL >= {lvl:.3f})")

# For realistic edges found in this research:
for wr_true in [0.55, 0.52]:
    # detect deviation from 0.5 with one-sample z-test
    h = 0.5
    za, zb = stats.norm.ppf(1 - 0.05), stats.norm.ppf(0.95)
    nn = ((za * np.sqrt(h * (1 - h)) + zb * np.sqrt(wr_true * (1 - wr_true))) / (wr_true - h)) ** 2
    print(f"  detect true WR={wr_true:.0%} vs coin flip (alpha=0.05, power=95%): "
          f"need n≈{int(np.ceil(nn)):,} trades")

print("\n=== D) Our best REAL pattern in this framing ===")
best = res.loc[res.p_side.idxmin()]
print(f"  {best.family} cell={int(best.cell)}: n={int(best.n)}, P(up)={best.wr_up:.4f}")
side = "DOWN" if best.wr_up < 0.5 else "UP"
wr_side = 1 - best.wr_up if side == "DOWN" else best.wr_up
print(f"  i.e. '{side}' side WR = {wr_side:.4f}. Even THIS best-of-264 pattern is only "
      f"{wr_side:.1%}, with BH-corrected q={best.q_bh:.1e}.")
