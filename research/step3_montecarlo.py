"""Step 3: Monte Carlo permutation null for the whole pattern-search pipeline.

Null hypothesis H0: no association between any feature pattern and next-candle
direction. We permute the target labels relative to the feature matrix
(preserving marginal distributions of both sides), rerun the IDENTICAL search,
and collect the null distribution of 'best WR' statistics. Empirical p-value =
how often pure chance beats the real-data result.
"""
import sys, json, time
sys.path.insert(0, "/home/alex/Project/up_down/research")
import numpy as np
import pandas as pd

f = pd.read_parquet("/home/alex/Project/up_down/research/features.parquet")
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

dir_ = f["dir"].to_numpy()
st = f["streak"].to_numpy()
fam = {}
for k in range(1, 7):
    hist = np.zeros(n_total, dtype=np.int64)
    for j in range(k):
        hist = hist * 2 + ((f["dir"].shift(j).fillna(1).to_numpy() == 1).astype(np.int64))
    fam[f"F1_hist{k}"] = hist.astype(np.int64)
fam["F2_streak"] = np.sign(st) * np.minimum(np.abs(st), 8)
fam["F3_rsi"] = np.digitize(f["rsi"], [10, 20, 30, 40, 60, 70, 80, 90])
fam["F4_shape"] = np.digitize(f["body_ratio"], [1/3, 2/3]) * 4 + np.digitize(f["close_pos"], [0.25, 0.75])
fam["F5_vol"] = np.digitize(f["vol_z"], [-1., 0., 1.])
fam["F6_hour"] = f["hour"].to_numpy()
fam["F7_band"] = np.digitize(f["band_pos"], [0.1, 0.3, 0.7, 0.9])
fam["F8_wick"] = (f["upper_wick"] > f["lower_wick"]).astype(int) * 2 + \
                 (np.maximum(f["upper_wick"], f["lower_wick"]) > 0.3).astype(int)
h2 = dir_ * 2 + (f["dir"].shift(1).fillna(1).to_numpy() == 1).astype(np.int64)
fam["F9_hist2xRSI"] = codes(np.digitize(f["rsi"], [35, 65]), h2)
fam["F10_hist2xBAND"] = codes(np.digitize(f["band_pos"], [0.25, 0.75]), h2)
fam["F11_streakxHOURBLK"] = codes(np.sign(st), f["hour"] // 4)
fam["F12_dirxVOL"] = codes(dir_, fam["F5_vol"])
h3 = dir_ * 4 + h2
fam["F13_hist3xBAND"] = codes(np.digitize(f["band_pos"], [0.25, 0.75]), h3)

# normalize codes once; precompute counts and up-counts for REAL data
prep = {}
for name, c in fam.items():
    c = (c - np.min(c)).astype(np.int64)
    K = int(c.max()) + 1
    tot = np.bincount(c, minlength=K).astype(np.float64)
    ups = np.bincount(c[y == 1], minlength=K).astype(np.float64)
    prep[name] = dict(code=c, K=K, tot=tot, ups=ups,
                      wr=np.divide(ups, tot, out=np.full(K, np.nan), where=tot > 0))

MIN_NS = [20, 100]

def pipeline_stats(rng, store_max, store_perfect, R_idx):
    yp = rng.permutation(y)
    best = {mn: 0.0 for mn in MIN_NS}
    perfect = {mn: 0 for mn in MIN_NS}
    perfect10 = 0
    for name, p in prep.items():
        up_p = np.bincount(p["code"][yp == 1], minlength=p["K"])
        wr = np.divide(up_p, p["tot"], out=np.zeros(p["K"]), where=p["tot"] > 0)
        dev = np.abs(wr - 0.5)
        for mn in MIN_NS:
            m = p["tot"] >= mn
            if m.any():
                d = dev[m].max()
                if d > best[mn]:
                    best[mn] = d
                perfect[mn] += int((dev[m] == 0.5).sum())
        m10 = p["tot"] >= 10
        perfect10 += int((dev[m10] == 0.5).sum())
    for mn in MIN_NS:
        store_max[mn].append(best[mn])
        store_perfect[mn].append(perfect[mn])
    store_perfect["n10"].append(perfect10)

R = 2000
rng = np.random.default_rng(42)
store_max = {mn: [] for mn in MIN_NS}
store_perfect = {mn: [] for mn in MIN_NS} | {"n10": []}

t0 = time.time()
for r in range(R):
    pipeline_stats(rng, store_max, store_perfect, r)
print(f"MC done: {R} permutations in {time.time()-t0:.1f}s")

# real-data statistics
real_best = {}
real_perfect = {}
for name, p in prep.items():
    for mn in MIN_NS:
        m = p["tot"] >= mn
        if m.any():
            d = float(np.abs(p["wr"][m] - 0.5).max())
            real_best[mn] = max(real_best.get(mn, 0.0), d)
            rp = p["ups"][m]; tt = p["tot"][m]
            real_perfect[mn] = real_perfect.get(mn, 0) + int(((rp == tt) | (rp == 0)).sum())

out = {"R": R}
for mn in MIN_NS:
    null_arr = np.array(store_max[mn])
    emp_p = float((np.array(store_max[mn]) >= real_best[mn]).mean())
    out[f"min_n={mn}"] = dict(
        real_max_dev_from_half=real_best[mn],
        null_mean=float(null_arr.mean()), null_p95=float(np.percentile(null_arr, 95)),
        null_p99=float(np.percentile(null_arr, 99)), null_max=float(null_arr.max()),
        empirical_p_value=emp_p,
        real_perfect_patterns=real_perfect[mn],
        null_perfect_patterns_mean=float(np.mean(store_perfect[mn])),
        null_perfect_patterns_max=int(np.max(store_perfect[mn])))
    print(f"\n=== min_n={mn} ===")
    print(f"  real max|WR-0.5|      : {real_best[mn]:.4f}")
    print(f"  null mean / p95 / p99 : {null_arr.mean():.4f} / {np.percentile(null_arr,95):.4f} / {np.percentile(null_arr,99):.4f}")
    print(f"  empirical p-value     : {emp_p:.4f}")
    print(f"  perfect patterns: real={real_perfect[mn]}, null mean={np.mean(store_perfect[mn]):.2f}, "
          f"null max={max(store_perfect[mn])}")

p10 = np.array(store_perfect["n10"])
out["perfect_n>=10"] = dict(real=0, null_mean=float(p10.mean()),
                            null_p95=float(np.percentile(p10, 95)), null_max=int(p10.max()))
print(f"\nPerfect patterns (n>=10): real=0, null mean={p10.mean():.2f}, p95={np.percentile(p10,95):.0f}, "
      f"max={p10.max()} across {R} shuffles")

json.dump(out, open("/home/alex/Project/up_down/research/step3_mc_summary.json", "w"), indent=1)
print("saved -> research/step3_mc_summary.json")
