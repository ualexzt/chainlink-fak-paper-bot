"""Train fair-value calibration model: P(leader wins | dist, time_left, vol, ...).

Outputs:
 1. Deployable fair-value grid (t_bucket x |dist| bin) train->test validated
 2. Pooled reliability + Brier on untouched test days
 3. Logistic regression per decision-time with holdout AUC
Saves LUT -> data/lut_fair_value.csv
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/alex/Project/up_down/research")

ROOT = Path("/home/alex/Project/up_down")


def main():
    df = pd.read_parquet(ROOT / "data" / "training_dataset.parquet")
    print(f"dataset rows={len(df)}, markets={df.groupby(['symbol','ts']).ngroups}")

    df["day"] = df["ts"] // 86400
    days = np.sort(df["day"].unique())
    cut_day = days[int(len(days) * 0.7)]

    # ---- features for LUT ----
    dist_bins = [-np.inf, -15, -10, -7, -5, -3, -2, -1, 1, 2, 3, 5, 7, 10, 15, np.inf]
    df["abs_bin"] = pd.cut(df["dist_bps"].abs(), [0, 1, 2, 3, 5, 7, 10, 15, np.inf])
    df["t_bucket"] = pd.cut(df["T"], [0, 240, 480, 720, 900],
                            labels=["T<=240", "240<T<=480", "480<T<=720", "T>720"])
    tr, te = df[df.day < cut_day].copy(), df[df.day >= cut_day].copy()
    print(f"train: {len(tr)} rows ({tr.ts.nunique()} markets) | test: {len(te)} rows "
          f"({te.ts.nunique()} markets)\n")

    key = ["t_bucket", "abs_bin"]
    lut = (tr.groupby(key, observed=True)
             .agg(n=("leader_won", "size"), wr=("leader_won", "mean"))
             .reset_index())
    # Bayesian shrinkage toward t_bucket mean (guards against small-cell overfit;
    # full-data test showed mid-range cells overestimate by ~3-7pp)
    K = 300.0
    bucket_means = tr.groupby("t_bucket", observed=True)["leader_won"].mean()
    lut["fair_value"] = lut.apply(
        lambda r: (r.n * r.wr + K * bucket_means[r.t_bucket]) / (r.n + K), axis=1)
    lut["raw_wr"] = lut["wr"]
    te_agg = (te.groupby(key, observed=True)
                .agg(n_te=("leader_won", "size"), wr_te=("leader_won", "mean"))
                .reset_index())
    merged = lut.merge(te_agg, on=key, how="left")

    print("=== FAIR VALUE GRID: P(leader wins) [train WR -> test WR] ===")
    print(f"{'T bucket':>12} {'|dist| bps':>13} {'n_tr':>6} {'WR_tr':>7} {'WR_te':>7} {'n_te':>6}")
    for _, r in merged.sort_values(["t_bucket", "abs_bin"]).iterrows():
        if r["n"] < 40:
            continue
        te_s = f"{r.wr_te:.3f}" if r.n_te == r.n_te and r.n_te >= 30 else "   -"
        nte_s = str(int(r.n_te)) if r.n_te == r.n_te else "-"
        lbl = str(r.abs_bin).replace("(", "").replace("]", "")
        print(f"{str(r.t_bucket):>12} {lbl:>13} {int(r['n']):>6} {r.wr:>7.3f} {te_s:>7} {nte_s:>6}")

    # pooled reliability of SHRUNK LUT predictions on test
    te_lut = te.merge(lut[[*key, "fair_value"]], on=key, how="left").dropna(subset=["fair_value"])
    print("\npredicted-vs-empirical (SHRUNK LUT, test, pooled by predicted-WR quintile):")
    if len(te_lut):
        te_lut = te_lut.copy()
        te_lut["bucket"] = pd.qcut(te_lut["fair_value"], 5, duplicates="drop")
        rel = te_lut.groupby("bucket", observed=True).agg(
            n=("leader_won", "size"), pred=("fair_value", "mean"), emp=("leader_won", "mean"))
        for b, r in rel.iterrows():
            print(f"  pred {b}: n={int(r.n)} pred={r.pred:.3f} emp={r.emp:.3f}")
        brier = float(((te_lut.leader_won - te_lut.fair_value) ** 2).mean())
        base_brier = float(((te_lut.leader_won - tr.leader_won.mean()) ** 2).mean())
        print(f"Brier (shrunk LUT, test): {brier:.4f} | baseline const-p Brier: {base_brier:.4f}")

    lut_out = lut[["t_bucket", "abs_bin", "n", "raw_wr", "fair_value"]]
    lut_out.to_csv(ROOT / "data" / "lut_fair_value.csv", index=False)
    print("LUT saved -> data/lut_fair_value.csv")

    # ---- logistic per T ----
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    df["abs_dist"] = df["dist_bps"].abs()
    tr2, te2 = df[df.day < cut_day], df[df.day >= cut_day]
    feats = ["abs_dist", "vol300_bps", "max_exc_bps", "crosses", "pre_vol_bps"]

    print("\n=== LOGISTIC per T (target=leader_won) ===")
    print(f"{'T':>4} {'n_tr':>6} {'n_te':>5} {'AUC_te':>7} {'Brier':>7} {'base':>7}")
    for T in sorted(df["T"].unique()):
        ttr, tte = tr2[tr2["T"] == T], te2[te2["T"] == T]
        if len(ttr) < 800 or len(tte) < 400:
            continue
        m = LogisticRegression(max_iter=500, C=0.5)
        m.fit(ttr[feats], ttr["leader_won"])
        p = m.predict_proba(tte[feats])[:, 1]
        auc = roc_auc_score(tte["leader_won"], p)
        br = float(np.mean((tte["leader_won"].to_numpy() - p) ** 2))
        base = float(np.mean((tte["leader_won"].to_numpy() - ttr["leader_won"].mean()) ** 2))
        print(f"{T:>4} {len(ttr):>6} {len(tte):>5} {auc:>7.3f} {br:>7.4f} {base:>7.4f}")


if __name__ == "__main__":
    main()
