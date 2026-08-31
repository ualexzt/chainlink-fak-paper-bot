"""Step 4: realistic prediction ceiling.

1. Walk-forward ML (expanding window, refit weekly, predict next week):
   LogisticRegression + HistGradientBoosting on lagged features.
   Compares accuracy to the 0.5 coin-flip baseline out-of-sample.
2. Mutual information between each discrete pattern family and next direction,
   in bits (perfect prediction would need 1 bit).
"""
import sys, json, time
sys.path.insert(0, "/home/alex/Project/up_down/research")
import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import log_loss

f = pd.read_parquet("/home/alex/Project/up_down/research/features.parquet")
y = (f["y"] == 1).to_numpy().astype(int)
n = len(y)

# ---------------- feature matrix ----------------
X = pd.DataFrame(index=f.index)
for k in range(1, 11):
    X[f"d{k}"] = f["dir"].shift(k)
    X[f"r{k}"] = f["ret"].shift(k)
X["rsi"] = f["rsi"]
X["body"] = f["body_ratio"]
X["cpos"] = f["close_pos"]
X["uw"] = f["upper_wick"]
X["lw"] = f["lower_wick"]
X["volz"] = f["vol_z"]
X["streak"] = np.tanh(f["streak"] / 5.0)
X["band"] = f["band_pos"]
h = f["hour"].to_numpy()
X["hour_sin"] = np.sin(2 * np.pi * h / 24)
X["hour_cos"] = np.cos(2 * np.pi * h / 24)
X = X.fillna(0.0)

# ---------------- walk-forward ----------------
WEEK = 2016                      # 5m bars per week
n_weeks = n // WEEK
start_week = 26                  # first fold trains on 26 weeks
fold_acc_lr, fold_acc_gb, fold_n = [], [], []
base_ll, lr_ll, gb_ll = [], [], []
t0 = time.time()
for w in range(start_week, n_weeks):
    tr_end = w * WEEK
    te_end = min((w + 1) * WEEK, n)
    Xtr, ytr = X.iloc[:tr_end], y[:tr_end]
    Xte, yte = X.iloc[tr_end:te_end], y[tr_end:te_end]
    if len(yte) < 500:
        continue
    lr = LogisticRegression(max_iter=300, C=0.1)
    lr.fit(Xtr, ytr)
    acc_lr = float((lr.predict(Xte) == yte).mean())
    p_lr = lr.predict_proba(Xte)[:, 1]
    gb = HistGradientBoostingClassifier(max_iter=150, learning_rate=0.06,
                                        max_depth=4, random_state=0)
    gb.fit(Xtr, ytr)
    acc_gb = float((gb.predict(Xte) == yte).mean())
    p_gb = gb.predict_proba(Xte)[:, 1]
    fold_acc_lr.append(acc_lr); fold_acc_gb.append(acc_gb); fold_n.append(len(yte))
    base_ll.append(log_loss(yte, np.full(len(yte), 0.5), labels=[0, 1]))
    lr_ll.append(log_loss(yte, np.clip(p_lr, 1e-6, 1 - 1e-6)))
    gb_ll.append(log_loss(yte, np.clip(p_gb, 1e-6, 1 - 1e-6)))

accs_lr = np.array(fold_acc_lr); accs_gb = np.array(fold_acc_gb)
wn = np.array(fold_n)
print("=== WALK-FORWARD (expanding, weekly refits) ===")
print(f"folds: {len(accs_lr)} weeks, total OOS predictions: {wn.sum()}")
print(f"LogReg : mean acc {accs_lr.mean():.4f} (weighted {np.average(accs_lr,weights=wn):.4f}), "
      f"range [{accs_lr.min():.4f}, {accs_lr.max():.4f}], beats 0.5 in {(accs_lr>0.5).sum()}/{len(accs_lr)} folds")
print(f"HistGB : mean acc {accs_gb.mean():.4f} (weighted {np.average(accs_gb,weights=wn):.4f}), "
      f"range [{accs_gb.min():.4f}, {accs_gb.max():.4f}], beats 0.5 in {(accs_gb>0.5).sum()}/{len(accs_gb)} folds")
se = np.sqrt(np.average((accs_lr - np.average(accs_lr, weights=wn))**2, weights=wn) / wn.sum())
print(f"LogReg weighted acc 95% CI: ±{1.96*se:.4f}")
print(f"log-loss: baseline {np.mean(base_ll):.6f}, LR {np.mean(lr_ll):.6f}, GB {np.mean(gb_ll):.6f}")
print(f"(time: {time.time()-t0:.0f}s)")

# ---------------- mutual information ----------------
def mi_bits(x_codes, K, yv):
    joint = np.zeros((K, 2))
    np.add.at(joint, (x_codes.astype(int), yv), 1)
    pxy = joint / joint.sum()
    px = pxy.sum(1, keepdims=True); py = pxy.sum(0, keepdims=True)
    nz = pxy > 0
    return float((pxy[nz] * np.log2(pxy[nz] / (px @ py)[nz])).sum())

print("\n=== MUTUAL INFORMATION (feature family -> next direction) ===")
dir_ = f["dir"].to_numpy()
fam_mi = {
    "prev dir": dir_,
    "RSI decile": np.digitize(f["rsi"], [10,20,30,40,60,70,80,90]),
    "hist last 3": dir_*4 + dir_.clip(0)*0 + ((pd.Series(dir_).shift(1).fillna(1).to_numpy()==1).astype(int)*2 + (pd.Series(dir_).shift(2).fillna(1).to_numpy()==1).astype(int)),
    "band position quintile": np.digitize(f["band_pos"], [0.1,0.3,0.7,0.9]),
    "vol regime quartile": np.digitize(f["vol_z"], [-1.,0.,1.]),
    "shape 12-cell": np.digitize(f["body_ratio"],[1/3,2/3])*4 + np.digitize(f["close_pos"],[0.25,0.75]),
    "hour of day": f["hour"].to_numpy(),
}
for nm, cser in fam_mi.items():
    c = np.asarray(cser); c = c - c.min()
    print(f"  {nm:26s}: {mi_bits(c, int(c.max())+1, y):.5f} bits")
print("\n(reference: perfect prediction requires exactly 1.0 bit)")

json.dump({
    "walk_forward": {"weeks": len(accs_lr),
                     "lr_mean": float(accs_lr.mean()), "lr_weighted": float(np.average(accs_lr, weights=wn)),
                     "gb_mean": float(accs_gb.mean()), "gb_weighted": float(np.average(accs_gb, weights=wn)),
                     "lr_beats_coin_folds": int((accs_lr > 0.5).sum()),
                     "gb_beats_coin_folds": int((accs_gb > 0.5).sum()),
                     "logloss_base": float(np.mean(base_ll)), "logloss_lr": float(np.mean(lr_ll)),
                     "logloss_gb": float(np.mean(gb_ll))},
    "mi_bits": {nm: mi_bits(np.asarray(c) - np.asarray(c).min(), int(np.asarray(c).max()) - int(np.asarray(c).min()) + 1, y)
                for nm, cser in fam_mi.items() for c in [cser]},
}, open("/home/alex/Project/up_down/research/step4_summary.json", "w"), indent=1)
