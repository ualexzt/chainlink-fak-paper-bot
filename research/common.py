"""Common utilities for candle prediction research."""
import numpy as np
import pandas as pd

DATA_PATH = "/home/alex/Project/up_down/data/btcusdt_5m.csv"


def load_data() -> pd.DataFrame:
    df = pd.read_csv(DATA_PATH, parse_dates=["open_time"])
    df = df.sort_values("open_time").reset_index(drop=True)
    return df


def data_quality(df: pd.DataFrame) -> dict:
    q = {}
    q["rows"] = len(df)
    q["start"] = str(df.open_time.iloc[0])
    q["end"] = str(df.open_time.iloc[-1])
    q["dup_open_times"] = int(df.open_time.duplicated().sum())
    gaps = df.open_time.diff().dropna()
    expected = pd.Timedelta(minutes=5)
    q["gaps"] = int((gaps != expected).sum())
    if q["gaps"]:
        bad = df.loc[gaps[gaps != expected].index, "open_time"]
        q["gap_examples"] = [str(t) for t in bad.head(5)]
    q["bad_ohlc"] = int(((df.high < df[["open", "close", "low"]].max(axis=1)) |
                         (df.low > df[["open", "close", "min"]].min(axis=1) if False else df[["open", "close"]].min(axis=1))).sum())
    q["zero_range"] = int((df.high == df.low).sum())
    return q


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """Feature matrix aligned so that row t contains info known at END of candle t,
    and target y[t] = direction of candle t+1."""
    f = pd.DataFrame(index=df.index)
    o, h, l, c, v = df.open, df.high, df.low, df.close, df.volume

    rng = (h - l).replace(0, np.nan)
    f["dir"] = np.sign(c - o).replace(0, 1).astype(int)          # +1 up / -1 down
    f["ret"] = c.pct_change().fillna(0.0)
    f["body_ratio"] = ((c - o).abs() / rng).clip(0, 1).fillna(0.0)
    f["close_pos"] = ((c - l) / rng).clip(0, 1).fillna(0.5)      # where close sits in range
    f["upper_wick"] = ((h - np.maximum(o, c)) / rng).clip(0, 1).fillna(0.0)
    f["lower_wick"] = ((np.minimum(o, c) - l) / rng).clip(0, 1).fillna(0.0)

    lv = np.log1p(v)
    f["vol_z"] = ((lv - lv.rolling(288, min_periods=50).mean()) /
                  lv.rolling(288, min_periods=50).std()).fillna(0.0)

    # streak: consecutive same-direction candles ending here
    d = f["dir"].to_numpy()
    streak = np.zeros(len(d), dtype=int)
    for i in range(1, len(d)):
        if d[i] == d[i - 1]:
            s = streak[i - 1]
            streak[i] = s + 1 if s > 0 else 2
        else:
            streak[i] = -1 if streak[i - 1] > 0 else streak[i - 1] - 1
    f["streak"] = streak  # +k = k consecutive ups, -k = k downs

    # RSI(14)
    delta = c.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
    loss_ = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rs = gain / loss_.replace(0, np.nan)
    f["rsi"] = (100 - 100 / (1 + rs)).fillna(50.0)

    # position within recent 4h (48 bars) high/low band
    hh = h.rolling(48, min_periods=24).max()
    ll = l.rolling(48, min_periods=24).min()
    band = (hh - ll).replace(0, np.nan)
    f["band_pos"] = ((c - ll) / band).clip(0, 1).fillna(0.5)

    f["hour"] = df.open_time.dt.hour

    # target: next candle direction
    f["y"] = f["dir"].shift(-1)
    f["next_ret"] = f["ret"].shift(-1)
    return f.iloc[:-1].reset_index(drop=True)  # drop last row (no target)


def wilson_ci(wins: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = wins / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (centre - half, centre + half)


def bh_qvalues(pvals: np.ndarray) -> np.ndarray:
    """Benjamini-Hochberg q-values."""
    n = len(pvals)
    order = np.argsort(pvals)
    ranked = pvals[order]
    qs = ranked * n / (np.arange(n) + 1)
    qs = np.minimum.accumulate(qs[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.minimum(qs, 1.0)
    return out
