"""Backfill historical Polymarket up/down markets metadata from Gamma API.
Writes one JSONL per symbol/timeframe into data/gamma/. Resumable.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path("/home/alex/Project/up_down")
OUT = ROOT / "data" / "gamma"
OUT.mkdir(parents=True, exist_ok=True)

UA = {"User-Agent": "Mozilla/5.0 (market research)"}

SYMBOLS = ["btc", "eth", "sol"]
TFS = {"15m": 900}


def get(url: str, retries: int = 3):
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == retries - 1:
                return None
            time.sleep(1.5 * (i + 1))
    return None


def main(days_back: int = 21):
    now = int(time.time())
    total_new, total_miss = 0, 0
    for sym in SYMBOLS:
        for tf, step in TFS.items():
            out_path = OUT / f"{sym}_{tf}.jsonl"
            seen = set()
            if out_path.exists():
                with open(out_path) as fh:
                    for line in fh:
                        try:
                            seen.add(json.loads(line)["slug"])
                        except Exception:
                            pass
            fp = open(out_path, "a", buffering=1)
            n_new = n_miss = 0
            # iterate from oldest to newest
            for k in range(days_back * 86400 // step, 0, -1):
                ts = ((now - k * step) // step) * step
                # skip windows that havent fully ended
                if ts + step > now - 120:
                    continue
                slug = f"{sym}-updown-{tf}-{ts}"
                if slug in seen:
                    continue
                j = get(f"https://gamma-api.polymarket.com/markets?slug={slug}&closed=true")
                if isinstance(j, list) and j:
                    m = j[0]
                    rec = {
                        "slug": m.get("slug"),
                        "ts": ts,
                        "tf": tf,
                        "conditionId": m.get("conditionId"),
                        "outcomes": m.get("outcomes"),
                        "outcomePrices": m.get("outcomePrices"),
                        "closed": m.get("closed"),
                        "startDate": m.get("startDateIso") or m.get("startDate"),
                        "endDate": m.get("endDateIso") or m.get("endDate"),
                        "volumeClob": m.get("volumeClob"),
                        "liquidityClob": m.get("liquidityClob"),
                    }
                    fp.write(json.dumps(rec) + "\n")
                    n_new += 1
                else:
                    n_miss += 1
                    if n_miss and n_miss % 20 == 0:
                        fp.write(json.dumps({"slug": slug, "ts": ts, "MISS": True}) + "\n")
                time.sleep(0.12)
            fp.close()
            print(f"{sym} {tf}: new={n_new} miss={n_miss}", flush=True)
            total_new += n_new
            total_miss += n_miss
    print(f"DONE total_new={total_new} total_miss={total_miss}")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 21)
