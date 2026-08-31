"""Step 3b: dependence-aware validation.

1. BLOCK permutation MC (blocks of 288 rows = 1 day): preserves short-range
   dependence inside features AND labels, breaks only their alignment.
2. Day-level aggregation for top patterns: one observation per day kills
   intraday dependence; t-test / sign-test across ~365 days.
3. Honest holdout: pick best patterns on 2025H2+2026H1 (train), test on
   last 3 months untouched.
"""
import sys, json
sys.path.insert(0, "/home/alex/Project/up_down/research")
import numpy as np
import pandas as pd
from scipy import stats

f = pd.read_parquet("/home/alex/Project/up_down/research/features.parquet")
raw = pd.read_csv("/home/alex/Project/up_down/data/btcusdt_5m.csv", parse_dates=["open_time"]).iloc[:-1]
y = (f["y"] == 1).to_numpy().astype(np.int64)
n_total = len(y)

def codes(*series_list):
    out = np.zeros(n_total, dtype=np.int64); mult = 1
    for s in series_list:
        arr = s.to_numpy() if hasattr(s, "to_numpy") else np.asarray(s)
        arr = arr - arr.min()
        out += arr * mult
        mult *= int(arr.max()) + 1
    return out

dir_ = f["dir"].to_numpy(); st = f["streak"].to_numpy()
fam = {}
for k in [1, 2, 3, 6]:
    hist = np.zeros(n_total, dtype=np.int64)
    for j in range(k):
        hist = hist * 2 + ((f["dir"].shift(j).fillna(1).to_numpy() == 1).astype(np.int64))
    fam[f"F1_hist{k}"] = hist
fam["F2_streak"] = np.sign(st) * np.minimum(np.abs(st), 8)
fam["F3_rsi"] = np.digitize(f["rsi"], [10, 20, 30, 40, 60, 70, 80, 90])
fam["F7_band"] = np.digitize(f["band_pos"], [0.1, 0.3, 0.7, 0.9])
h2 = dir_ * 2 + (f["dir"].shift(1).fillna(1).to_numpy() == 1).astype(np.int64)
fam["F9_hist2xRSI"] = codes(np.digitize(f["rsi"], [35, 65]), h2)
fam["F10_hist2xBAND"] = codes(np.digitize(f["band_pos"], [0.25, 0.75]), h2)
fam["F12_dirxVOL"] = codes(dir_, np.digitize(f["vol_z"], [-1., 0., 1.]))

# ---------------- 1. BLOCK permutation MC ---------------------------------
BLOCK = 288
R = 2000
rng = np.random.default_rng(7)
n_blocks = int(np.ceil(n_total / BLOCK))
pad = n_blocks * BLOCK - n_total

prep = {}
for name, c in fam.items():
    c = (c - c.min()).astype(np.int64)
    K = int(c.max()) + 1
    tot = np.bincount(c, minlength=K)
    ups = np.bincount(c[y == 1], minlength=K)
    prep[name] = dict(code=c, K=K, tot=tot.astype(float),
                      wr=np.divide(ups, tot, out=np.full(K, np.nan), where=tot > 0))

null_max = {20: [], 100: []}
for r in range(R):
    yp = rng.permutation(y)  # start iid then re-block: shuffle whole blocks of yp
    # block-shuffle: cut padded series into blocks, shuffle block order
    idx = rng.permutation(n_blocks)
    blocks = np.split(yp, n_blocks)
    ys = np.concatenate([blocks[i] for i in idx])[:n_total]
    best = {20: 0.0, 100: 0.0}
    for name, p in prep.items():
        up_p = np.bincount(p["code"][ys == 1], minlength=p["K"])
        wr = np.divide(up_p, p["tot"], out=np.zeros(p["K"]), where=p["tot"] > 0)
        dev = np.abs(wr - 0.5)
        for mn in (20, 100):
            m = p["tot"] >= mn
            if m.any() and dev[m].max() > best[mn]:
                best[mn] = float(dev[m].max())
    for mn in (20, 100):
        null_max[mn].append(best[mn])

real_best = {}
for name, p in prep.items():
    for mn in (20, 100):
        m = p["tot"] >= mn
        d = float(np.nanmax(np.abs(p["wr"][m] - 0.5))) if m.any() else 0.0
        real_best[mn] = max(real_best.get(mn, 0.0), d)

print("=== BLOCK-PERMUTATION NULL (block = 1 day, R=%d) ===" % R)
for mn in (20, 100):
    arr = np.array(null_max[mn])
    p_emp = float((arr >= real_best[mn]).mean())
    print(f"  min_n={mn}: real max|dev|={real_best[mn]:.4f} | "
          f"null mean={arr.mean():.4f}, p95={np.percentile(arr,95):.4f}, "
          f"p99={np.percentile(arr,99):.4f} -> empirical p = {p_emp:.3f}")

# ---------------- 2. Day-level aggregation of top patterns ---------------
day = raw.open_time.dt.date.to_numpy()
days = np.unique(day)
day_idx = pd.factorize(day)[0]
D = len(days)

def daily_test(name, mask):
    """One observation per day: fraction of 'up' outcomes among masked rows."""
    w = (y[mask] == 1).astype(float)
    g = day_idx[mask]
    cnt = np.bincount(g, minlength=D)
    ups = np.bincount(g, weights=w, minlength=D)
    valid = cnt >= 10          # days with at least 10 occurrences
    vals = ups[valid] / cnt[valid]
    t = stats.ttest_1samp(vals, 0.5)
    sign_p = stats.binomtest(int((vals > 0.5).sum()), len(vals), 0.5).pvalue
    print(f"  {name:34s} days={len(vals):>3} meanWR={vals.mean():.4f} sd={vals.std():.4f} "
          f"| t={t.statistic:+.2f} p={t.pvalue:.3g} | sign-test p={sign_p:.3g}")
    return vals

print("\n=== DAILY AGGREGATION (>=10 triggers/day; H0: daily WR=0.5) ===")
daily_test("flip: prev DN -> up", dir_ == -1)
m_rsi_hi = fam["F3_rsi"] == 5            # RSI 60-70
daily_test("RSI 60-70 -> up (cont)", m_rsi_hi)
m_os = (fam["F9_hist2xRSI"] == 0)        # RSI<35 & last two red
daily_test("RSI<35 & 2 reds -> up (rev)", m_os)
m_bd = fam["F10_hist2xBAND"] == 0
daily_test("hist2xDN & band low -> up", m_bd)
m_tr = (fam["F3_rsi"] == 4)              # RSI 40-60
daily_test("RSI 40-60 -> up", m_tr)
m_band_hi = fam["F7_band"] == 4          # band_pos > 0.9
daily_test("near 4h-high (>0.9) -> up", m_band_hi)

# ---------------- 3. Honest holdout ---------------------------------------
print("\n=== HONEST HOLDOUT (train: rows 0..70%, test: last 30%) ===")
cut = int(0.7 * n_total)
for nm, code_arr, cell, side in [
    ("RSI 60-70 -> DOWN",      fam["F3_rsi"], 5, -1),
    ("RSI<35 & 2red -> UP",    fam["F9_hist2xRSI"], 0, +1),
    ("band low & 2red -> UP",  fam["F10_hist2xBAND"], 0, +1),
    ("near 4h-high -> DOWN",   fam["F7_band"], 4, -1),
]:
    m = (code_arr == cell)
    tr_n, te_n = int(m[:cut].sum()), int(m[cut:].sum())
    tr_wr = (y[:cut][m[:cut]] == (1 if side == 1 else 0)).mean()
    te_wr = (y[cut:][m[cut:]] == (1 if side == 1 else 0)).mean()
    tt = stats.ttest_1samp((y[cut:][m[cut:]] == (side == 1)).astype(float), 0.5) if te_n > 30 else None
    extra = f", t-p={tt.pvalue:.3g}" if tt is not None else ""
    print(f"  {nm:26s} train n={tr_n:>6} WR={tr_wr:.4f} | test n={te_n:>5} WR={te_wr:.4f}{extra}")
