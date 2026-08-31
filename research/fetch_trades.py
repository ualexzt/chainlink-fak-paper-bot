"""Fetch historical trades (prints) for backfilled markets from Polymarket Data-API.
Saves per-symbol parquet under data/trades/. Resumable per conditionId.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

import pandas as pd

ROOT = Path("/home/alex/Project/up_down")
GAMMA = ROOT / "data" / "gamma"
OUT = ROOT / "data" / "trades"
OUT.mkdir(parents=True, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (market research)"}


def get(url):
    for i in range(3):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception:
            time.sleep(1.0 * (i + 1))
    return None


def fetch_market_trades(cid: str):
    rows, offset = [], 0
    while True:
        j = get(f"https://data-api.polymarket.com/trades?market={cid}&limit=500&offset={offset}")
        if not isinstance(j, list) or not j:
            break
        rows.extend(j)
        if len(j) < 500 or offset > 20000:
            break
        offset += 500
        time.sleep(0.08)
    return rows


def main(shard: int = 0, nparts: int = 1, ts_min: int = 0):
    out_f = OUT / f"shard_{shard}of{nparts}.parquet"
    tmp_f = Path(str(out_f) + ".tmp")
    done = set()
    if out_f.exists():
        done = set(pd.read_parquet(out_f, columns=["conditionId"]).conditionId.unique())
        print(f"shard {shard}: resuming, {len(done)} done", flush=True)
    frames = []
    todo_total = 0
    for gf in sorted(GAMMA.glob("*_15m.jsonl")):
        sym = gf.stem.split("_")[0]
        with open(gf) as fh:
            for line in fh:
                try:
                    r = json.loads(line)
                    cid = r.get("conditionId")
                    if r.get("MISS") or not cid or not r.get("outcomePrices"):
                        continue
                    if int(cid[-16:], 16) % nparts != shard:   # deterministic shard
                        continue
                    if cid in done or r["ts"] < ts_min:
                        continue
                    tr = fetch_market_trades(cid)
                    rows = []
                    for x in tr or []:
                        rows.append({
                            "conditionId": cid, "symbol": sym, "mkt_ts": r["ts"],
                            "t": int(x.get("timestamp", 0)),
                            "outcome": x.get("outcome"), "side": x.get("side"),
                            "price": float(x.get("price") or 0),
                            "size": float(x.get("size") or 0),
                        })
                    frames.extend(rows)
                    todo_total += 1
                    time.sleep(0.04)
                    if todo_total % 50 == 0:
                        print(f"shard {shard}: {todo_total} markets, {len(frames)} prints", flush=True)
                        pd.DataFrame(frames).to_parquet(tmp_f, index=False)
                except Exception as e:
                    print(f"ERR {cid}: {e}", flush=True)
        # checkpoint per symbol
    if frames:
        df = pd.DataFrame(frames)
        old = pd.read_parquet(out_f) if out_f.exists() else None
        df = pd.concat([old, df]).drop_duplicates(["conditionId", "t", "size", "price"]) if old is not None else df
        df.to_parquet(out_f, index=False)
        print(f"shard {shard}: saved {len(df)} prints -> {out_f.name}", flush=True)


if __name__ == "__main__":
    s = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 1
    tmin = int(sys.argv[3]) if len(sys.argv) > 3 else 0
    main(s, n, tmin)
