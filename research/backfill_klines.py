"""Backfill Binance 1s klines per day into parquet files. Resumable."""
import sys
import time
import json
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path("/home/alex/Project/up_down")
OUT = ROOT / "data" / "binance"
OUT.mkdir(parents=True, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (market research)"}


def get_klines(symbol: str, start_ms: int, end_ms: int):
    url = (f"https://api.binance.com/api/v3/klines?symbol={symbol}"
           f"&interval=1s&startTime={start_ms}&endTime={end_ms}&limit=1000")
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(1.2 * (i + 1))
    return None


def main(days_back: int = 21, symbols=("BTCUSDT", "ETHUSDT", "SOLUSDT")):
    now = int(time.time())
    today0 = (now // 86400) * 86400
    for sym in symbols:
        sym_dir = OUT / sym
        sym_dir.mkdir(exist_ok=True)
        for d in range(days_back - 1, -1, -1):   # oldest -> newest
            day0 = today0 - d * 86400            # UTC midnight
            f = sym_dir / f"{time.strftime('%Y-%m-%d', time.gmtime(day0))}.parquet"
            if f.exists():
                continue
            frames = []
            cur = day0 * 1000
            end = (day0 + 86400) * 1000
            while cur < end:
                batch = get_klines(sym, cur, end - 1)
                if not batch:
                    break
                frames.append(batch)
                last_close_t = batch[-1][6]      # close_time ms
                nxt = last_close_t + 1
                if nxt <= cur:
                    break
                cur = nxt
                if len(batch) < 1000:
                    break
                time.sleep(0.08)
            if frames:
                rows = [r for b in frames for r in b]
                df = pd.DataFrame(rows, columns=[
                    "open_time", "open", "high", "low", "close", "volume",
                    "close_time", "qav", "trades", "tbb", "tbq", "ignore"])
                df = df.drop_duplicates("open_time").sort_values("open_time")
                # keep only this UTC day
                df = df[(df.open_time >= day0 * 1000) & (df.open_time < end)]
                df.to_parquet(f, index=False)
                print(f"{sym} {f.name}: {len(df)} rows", flush=True)
            else:
                print(f"{sym} {day0}: EMPTY", flush=True)


if __name__ == "__main__":
    db = int(sys.argv[1]) if len(sys.argv) > 1 else 21
    syms = sys.argv[2].split(",") if len(sys.argv) > 2 else ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    main(db, syms)
