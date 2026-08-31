#!/usr/bin/env python3
"""Polymarket up/down research recorder v2.

Per-second aligned rows for divergence observer & future backtests:
  - Chainlink TWAP-60/30 + spot oracle   (Polymarket RTDS relay, no auth)
  - Binance 1s close + reconstructed TWAP60
  - Active market top-of-book            (CLOB WebSocket market channel)
  - Derived: strikes (CL & Binance), dist bps, basis bps, LUT fair value

Output: DATA_DIR/YYYY-MM-DD.jsonl (UTC daily rotation), heartbeat file.
"""
import asyncio
import contextlib
import json
import logging
import math
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import websockets

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("recorder")

SYMBOLS = [s.strip().lower() for s in os.getenv("SYMBOLS", "btc,eth,sol").split(",")]
TFS = [(p.split(":")[0], int(p.split(":")[1]))
       for p in os.getenv("TFS", "15m:900").split(",")]
LUT_TFS = set(s.strip() for s in os.getenv("LUT_TFS", "15m").split(","))
DATA_DIR = Path(os.getenv("DATA_DIR", "/data"))
AGG_INTERVAL = float(os.getenv("AGG_INTERVAL", "1.0"))
RTDS_URL = os.getenv("RTDS_URL", "wss://ws-live-data.polymarket.com")
BINANCE_WS = os.getenv(
    "BINANCE_WS",
    "wss://stream.binance.com:9443/stream?streams="
    + "/".join(f"{s}usdt@kline_1s" for s in SYMBOLS))
CLOB_WS = os.getenv("CLOB_WS", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
LUT_PATH = os.getenv("LUT_PATH", "")
UA = {"User-Agent": "Mozilla/5.0 (research-recorder)"}
E18 = 10 ** 18
BOOK_STALE_SEC = 45.0
RTDS_STALE_SEC = float(os.getenv("RTDS_STALE_SEC", "10"))
RTDS_START_CAPTURE_SEC = 10

DATA_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- LUT ------
def load_lut():
    if not LUT_PATH or not Path(LUT_PATH).exists():
        log.warning("LUT not found (%s) -> fair_value will be null", LUT_PATH)
        return None
    import csv
    buckets = {}
    with open(LUT_PATH) as fh:
        for r in csv.DictReader(fh):
            lo = float(r["abs_bin"].replace("(", "").split(",")[0])
            buckets.setdefault(r["t_bucket"], []).append((lo, float(r["fair_value"])))
    for k in buckets:
        buckets[k].sort()
    def lookup(tb, ad):
        val = None
        for lo, fv in buckets.get(tb, []):
            if ad >= lo:
                val = fv
        return val
    return lookup

LUT = load_lut()

def t_bucket_for(age: int) -> str:
    if age <= 240:
        return "T<=240"
    if age <= 480:
        return "240<T<=480"
    if age <= 720:
        return "480<T<=720"
    return "T>720"


def stale_rtds_symbols(last_seen, symbols, now, timeout):
    return sorted(
        symbol
        for symbol in symbols
        if symbol not in last_seen or now - last_seen[symbol] > timeout
    )


def accept_cl_observation(cached, value, obs_ts_ms):
    try:
        price = float(value)
        timestamp = int(obs_ts_ms)
    except (TypeError, ValueError):
        return False
    if not math.isfinite(price) or price <= 0.0 or timestamp <= 0:
        return False
    return cached is None or timestamp > int(cached[1])


def capture_resolver_start(current, value, obs_ts_ms, mkt_ts):
    if current is not None:
        return current
    if not accept_cl_observation(None, value, obs_ts_ms):
        return None
    start_ms = int(mkt_ts) * 1000
    if start_ms <= int(obs_ts_ms) <= start_ms + RTDS_START_CAPTURE_SEC * 1000:
        return float(value)
    return None


def first_resolver_start(current, history, mkt_ts):
    if current is not None:
        return current
    for obs_ts_ms, value in history:
        captured = capture_resolver_start(None, value, obs_ts_ms, mkt_ts)
        if captured is not None:
            return captured
    return None


def cl_age_and_fresh(now_ms, obs_ts_ms, stale_seconds=RTDS_STALE_SEC):
    if obs_ts_ms is None:
        return None, False
    try:
        age_ms = int(now_ms) - int(obs_ts_ms)
    except (TypeError, ValueError):
        return None, False
    return age_ms, 0 <= age_ms <= stale_seconds * 1000


def resolver_metrics(current, start):
    current_value = float(current)
    start_value = float(start)
    distance = current_value - start_value
    distance_bps = distance / start_value * 10_000
    leader = "UP" if distance > 0.0 else "DOWN" if distance < 0.0 else "TIE"
    return distance, distance_bps, leader


def resolver_momentum_5s_bps(history):
    if len(history) < 2:
        return None
    latest_ts, latest_value = history[-1]
    cutoff = int(latest_ts) - 5_000
    for prior_ts, prior_value in reversed(list(history)[:-1]):
        if int(prior_ts) <= cutoff and float(prior_value) > 0.0:
            return (float(latest_value) / float(prior_value) - 1.0) * 10_000
    return None


def process_rtds_frame(raw, state, symbols, now_ms=None):
    if not raw or raw == "PONG":
        return set()
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return set()
    messages = decoded if isinstance(decoded, list) else [decoded]
    refreshed = set()
    topic_kinds = {
        "crypto_prices_twap_sixty": "twap60",
        "crypto_prices_twap_thirty": "twap30",
        "crypto_prices_chainlink": "spot",
    }
    for message in messages:
        if not isinstance(message, dict):
            continue
        payload = message.get("payload") or {}
        symbol = (payload.get("symbol") or "").split("/")[0].lower()
        kind = topic_kinds.get(message.get("topic"))
        if symbol not in symbols or kind is None:
            continue
        try:
            if payload.get("full_accuracy_value") is not None:
                value = int(payload["full_accuracy_value"]) / E18
            else:
                value = float(payload["value"])
            obs_ts_ms = int(payload.get("timestamp") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        cached = state.cl.get((symbol, kind))
        if not accept_cl_observation(cached, value, obs_ts_ms):
            continue
        if now_ms is not None and not cl_age_and_fresh(now_ms, obs_ts_ms)[1]:
            continue
        state.cl[(symbol, kind)] = (value, obs_ts_ms)
        if kind == "twap60":
            state.cl_history.setdefault(symbol, deque(maxlen=120)).append(
                (obs_ts_ms, value)
            )
            refreshed.add(symbol)
    return refreshed


def resolver_row_fields(state, symbol, tf, mkt_ts, now_ms, cl60):
    obs_ts_ms = cl60[1] if cl60 else None
    age_ms, fresh = cl_age_and_fresh(now_ms, obs_ts_ms)
    key = (symbol, tf, mkt_ts)
    if fresh and cl60:
        start = first_resolver_start(
            state.resolver_starts.get(key),
            state.cl_history.get(symbol, ()),
            mkt_ts,
        )
        if start is not None:
            state.resolver_starts[key] = start
    start = state.resolver_starts.get(key)
    fields = {
        "cl_age_ms": age_ms,
        "cl_fresh": fresh,
        "resolver_start_twap": start,
        "resolver_distance": None,
        "resolver_distance_bps": None,
        "resolver_leader": None,
        "resolver_momentum_5s_bps": None,
    }
    if fresh and cl60:
        fields["resolver_momentum_5s_bps"] = resolver_momentum_5s_bps(
            state.cl_history.get(symbol, ())
        )
        if start is not None:
            distance, distance_bps, leader = resolver_metrics(cl60[0], start)
            fields.update(
                {
                    "resolver_distance": distance,
                    "resolver_distance_bps": distance_bps,
                    "resolver_leader": leader,
                }
            )
    return fields

# ----------------------------------------------------------- state --------
class State:
    def __init__(self):
        self.cl = {}          # (symbol,'twap60'|'twap30'|'spot') -> (value,obs_ts_ms)
        self.bnb_close = {}   # symbol -> deque of 1s closes
        self.books = {}       # asset_id -> {'lv':{'b':{p:q},'a':{p:q}}, 'ts':...}
        self.markets = {}     # asset_id -> meta
        self.strikes = {}     # (sym,tf,mkt_ts) -> {'cl':v,'bnb':v}
        self.cl_history = {symbol: deque(maxlen=120) for symbol in SYMBOLS}
        self.resolver_starts = {}  # (sym,tf,mkt_ts) -> first fresh TWAP-60 value

S = State()
for s in SYMBOLS:
    S.bnb_close[s] = deque(maxlen=120)

def _best(lv):
    bb = max(lv["b"]) if lv["b"] else None
    ba = min(lv["a"]) if lv["a"] else None
    return bb, ba

def book_of(aid):
    st = S.books.get(aid)
    if not st or time.time() - st["ts"] > BOOK_STALE_SEC:
        return None, None, None, None
    lv = st.get("lv") or {"b": {}, "a": {}}
    bb = st.get("bb") or (max(lv["b"]) if lv["b"] else None)
    ba = st.get("ba") or (min(lv["a"]) if lv["a"] else None)
    bq = lv["b"].get(bb) if bb is not None else None
    aq = lv["a"].get(ba) if ba is not None else None
    return bb, ba, bq, aq

# ------------------------------------------------------------- RTDS -------
async def rtds_task():
    # RTDS keeps only ONE filter per topic -> subscribe unfiltered, filter client-side
    subs = [{"topic": t, "type": "update"}
            for t in ("crypto_prices_twap_sixty", "crypto_prices_twap_thirty",
                      "crypto_prices_chainlink")]
    frame = json.dumps({"action": "subscribe", "subscriptions": subs})
    while True:
        try:
            async with websockets.connect(RTDS_URL, ping_interval=None) as ws:
                await ws.send(frame)
                log.info("RTDS subscribed (%d topics)", len(subs))
                connected_at = time.monotonic()
                last_fresh_twap60 = {symbol: connected_at for symbol in SYMBOLS}
                async def pinger():
                    while True:
                        await asyncio.sleep(5)
                        with contextlib.suppress(Exception):
                            await ws.send("PING")
                pt = asyncio.create_task(pinger())
                try:
                    while True:
                        try:
                            raw = await asyncio.wait_for(
                                ws.recv(), timeout=max(0.1, min(1.0, RTDS_STALE_SEC))
                            )
                        except asyncio.TimeoutError:
                            raw = None
                        wall_now_ms = int(time.time() * 1000)
                        updated = process_rtds_frame(
                            raw, S, SYMBOLS, now_ms=wall_now_ms
                        )
                        monotonic_now = time.monotonic()
                        for symbol in updated:
                            observation = S.cl.get((symbol, "twap60"))
                            if observation and cl_age_and_fresh(
                                wall_now_ms, observation[1]
                            )[1]:
                                last_fresh_twap60[symbol] = monotonic_now
                        stale = stale_rtds_symbols(
                            last_fresh_twap60,
                            SYMBOLS,
                            now=monotonic_now,
                            timeout=RTDS_STALE_SEC,
                        )
                        if stale:
                            raise RuntimeError(
                                "stale TWAP-60 symbols: " + ",".join(stale)
                            )
                finally:
                    pt.cancel()
        except Exception as e:
            log.warning("RTDS down: %s -> retry 5s", e)
            await asyncio.sleep(5)

# ---------------------------------------------------------- Binance -------
async def binance_task():
    while True:
        try:
            async with websockets.connect(BINANCE_WS) as ws:
                log.info("Binance WS connected")
                async for raw in ws:
                    m = json.loads(raw)
                    d = m.get("data") or {}
                    k = d.get("k") or {}
                    if k.get("x"):
                        sym = k["s"].lower().replace("usdt", "")
                        S.bnb_close[sym].append(float(k["c"]))
        except Exception as e:
            log.warning("Binance WS down: %s -> retry 5s", e)
            await asyncio.sleep(5)

# ------------------------------------------------------------ Gamma -------
async def gamma_task(session):
    while True:
        try:
            now = int(time.time())
            max_step = max(st for _, st in TFS)
            for sym in SYMBOLS:
                for tf, step in TFS:
                    base = (now // step) * step
                    for ts in (base, base + step):
                        url = (f"https://gamma-api.polymarket.com/markets"
                               f"?slug={sym}-updown-{tf}-{ts}&closed=false")
                        async with session.get(url) as r:
                            j = await r.json(content_type=None)
                        if not (isinstance(j, list) and j):
                            continue
                        mk = j[0]
                        if not mk.get("acceptingOrders"):
                            continue
                        ids = mk.get("clobTokenIds")
                        ids = json.loads(ids) if isinstance(ids, str) else ids
                        outs = mk.get("outcomes")
                        outs = json.loads(outs) if isinstance(outs, str) else outs
                        if "Up" not in outs:
                            continue
                        i_up = outs.index("Up")
                        meta = {"symbol": sym, "tf": tf, "mkt_ts": ts,
                                "up_id": ids[i_up], "down_id": ids[1 - i_up]}
                        for aid in (meta["up_id"], meta["down_id"]):
                            old = S.markets.get(aid)
                            if not old or old["mkt_ts"] < ts:
                                S.markets[aid] = meta
            for aid in list(S.markets):
                if S.markets[aid]["mkt_ts"] + max_step < now - 300:
                    S.markets.pop(aid, None)
                    S.books.pop(aid, None)
        except Exception as e:
            log.warning("gamma error: %s", e)
        await asyncio.sleep(20)

# --------------------------------------------------------- CLOB books -----
async def clob_books_task():
    """Maintains S.books from the CLOB market channel (snapshots+price changes)."""
    while True:
        try:
            ids = list(S.markets.keys())
            if not ids:
                await asyncio.sleep(2)
                continue
            async with websockets.connect(CLOB_WS, ping_interval=None,
                                  max_queue=int(os.getenv("WS_QUEUE", "2048")), compression=None) as ws:
                await ws.send(json.dumps({"assets_ids": ids, "type": "market"}))
                log.info("CLOB WS subscribed (%d assets)", len(ids))
                known = set(ids)
                last_check = time.time()
                async def pinger():
                    while True:
                        await asyncio.sleep(10)
                        with contextlib.suppress(Exception):
                            await ws.send("PING")
                pt = asyncio.create_task(pinger())
                try:
                    async for raw in ws:
                        if raw == "PONG" or not raw:
                            continue
                        now = time.time()
                        if now - last_check > 20:
                            last_check = now
                            fresh = [a for a in S.markets if a not in known]
                            if fresh:
                                log.info("CLOB resubscribe (+%d assets)", len(fresh))
                                break                       # reconnect w/ full list
                        try:
                            events = json.loads(raw)
                        except Exception:
                            continue
                        if isinstance(events, dict):
                            events = [events]
                        for ev in events:
                            if not isinstance(ev, dict):
                                continue
                            # -- legacy flat schema (live CLOB WS) ------------
                            if "price_changes" in ev:
                                for ch in ev["price_changes"]:
                                    aid = ch.get("asset_id")
                                    if not aid:
                                        continue
                                    st = S.books.setdefault(
                                        aid, {"lv": {"b": {}, "a": {}},
                                              "ts": 0, "bb": None, "ba": None})
                                    lv = st["lv"]
                                    pr = float(ch["price"])
                                    sz = float(ch["size"])
                                    key = "b" if ch.get("side") == "BUY" else "a"
                                    if sz <= 0:
                                        lv[key].pop(pr, None)
                                    else:
                                        lv[key][pr] = sz
                                    bbb, baa = ch.get("best_bid"), ch.get("best_ask")
                                    st["bb"] = float(bbb) if bbb not in (None, "") \
                                        else _best(lv)[0]
                                    st["ba"] = float(baa) if baa not in (None, "") \
                                        else _best(lv)[1]
                                    st["ts"] = time.time()
                                continue
                            # -- official envelope + legacy book ----------------
                            et = ev.get("type", "") or ev.get("event_type", "")
                            pl = ev.get("payload") or ev
                            aid = pl.get("tokenId") or pl.get("asset_id")
                            if not aid or et != "book":
                                continue
                            st = S.books.setdefault(
                                aid, {"lv": {"b": {}, "a": {}},
                                      "ts": 0, "bb": None, "ba": None})
                            lb, la = {}, {}
                            for x in pl.get("bids") or []:
                                pr = float(x["price"] if isinstance(x, dict) else x[0])
                                sz = float(x["size"] if isinstance(x, dict) else x[1])
                                lb[pr] = sz
                            for x in pl.get("asks") or []:
                                pr = float(x["price"] if isinstance(x, dict) else x[0])
                                sz = float(x["size"] if isinstance(x, dict) else x[1])
                                la[pr] = sz
                            st["lv"] = {"b": lb, "a": la}
                            st["bb"], st["ba"] = _best(st["lv"])
                            st["ts"] = time.time()
                finally:
                    pt.cancel()
        except Exception as e:
            log.warning("CLOB WS down: %s -> retry 5s", e)
            await asyncio.sleep(5)
        # prune stale books
        now = time.time()
        for aid in list(S.books):
            if aid not in S.markets or now - S.books[aid]["ts"] > 600:
                S.books.pop(aid, None)

# ------------------------------------------------------- aggregator -------
async def aggregator():
    out_path = None
    fh = None
    while True:
        try:
            nowms = int(time.time() * 1000)
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            p = DATA_DIR / f"{day}.jsonl"
            if p != out_path:
                if fh:
                    fh.close()
                fh = open(p, "a", buffering=1)
                out_path = p
            step_by_tf = dict(TFS)
            for key in list(S.resolver_starts):
                _, tf, mkt_ts = key
                if mkt_ts + step_by_tf[tf] < nowms // 1000 - 300:
                    S.resolver_starts.pop(key, None)
            for sym in SYMBOLS:
                cl60 = S.cl.get((sym, "twap60"))
                cl30 = S.cl.get((sym, "twap30"))
                spot = S.cl.get((sym, "spot"))
                dq = S.bnb_close[sym]
                bnb60 = sum(dq) / len(dq) if len(dq) >= 60 else None
                for tf, step in TFS:
                    mkt_ts = (nowms // 1000 // step) * step
                    age = nowms // 1000 - mkt_ts
                    st_key = (sym, tf, mkt_ts)
                    row = {
                        "ts": nowms // 1000, "symbol": sym, "tf": tf, "mkt_ts": mkt_ts,
                        "age": age,
                        "cl_twap60": cl60[0] if cl60 else None,
                        "cl_twap30": cl30[0] if cl30 else None,
                        "cl_spot": spot[0] if spot else None,
                        "cl_obs_ts": cl60[1] if cl60 else None,
                        "bnb_close": dq[-1] if dq else None,
                        "bnb_twap60": bnb60,
                    }
                    row.update(resolver_row_fields(S, sym, tf, mkt_ts, nowms, cl60))
                    st = S.strikes.get(st_key)
                    if st is None and age >= 59 and cl60 and bnb60:
                        S.strikes[st_key] = {"cl": cl60[0], "bnb": bnb60}
                        st = S.strikes[st_key]
                    if st:
                        row["strike_cl"] = st["cl"]
                        row["strike_bnb"] = st["bnb"]
                        if row["cl_twap60"]:
                            d_cl = (row["cl_twap60"] / st["cl"] - 1) * 1e4
                            row["dist_cl_bps"] = d_cl
                            # LUT grid trained per-timeframe; apply only where valid
                            if LUT and tf in LUT_TFS:
                                lead = "UP" if d_cl >= 0 else "DOWN"
                                row["leader_cl"] = lead
                                row["fair_leader_lut"] = LUT(
                                    t_bucket_for(min(age, 869)), abs(d_cl))
                        if bnb60:
                            row["dist_bnb_bps"] = (bnb60 / st["bnb"] - 1) * 1e4
                        if cl60 and bnb60:
                            row["basis_bps"] = (bnb60 / cl60[0] - 1) * 1e4
                    for aid, meta in S.markets.items():
                        if meta["symbol"] == sym and meta["tf"] == tf \
                                and meta["mkt_ts"] == mkt_ts:
                            bb, ba, bq, aq = book_of(aid)
                            pref = "up_" if aid == meta["up_id"] else "dn_"
                            row[pref + "bid"], row[pref + "bidq"] = bb, bq
                            row[pref + "ask"], row[pref + "askq"] = ba, aq
                    if row.get("up_bid") is not None and row.get("dn_bid") is not None:
                        row["sum_bid"] = row["up_bid"] + row["dn_bid"]
                    if row.get("up_ask") is not None and row.get("dn_ask") is not None:
                        row["sum_ask"] = row["up_ask"] + row["dn_ask"]
                    fh.write(json.dumps(row) + "\n")
            (DATA_DIR / ".heartbeat").write_text(str(int(time.time())))
            if int(time.time()) % 30 < AGG_INTERVAL:
                filled = sum(1 for s_ in S.books.values() if s_["lv"]["b"] or s_["lv"]["a"])
                fresh = sum(1 for s_ in S.books.values()
                            if time.time() - s_["ts"] < 120)
                per = {}
                for aid_, mt_ in S.markets.items():
                    k_ = (mt_["symbol"], mt_["tf"], mt_["mkt_ts"])
                    has_book = aid_ in S.books
                    e_ = per.setdefault(k_, [0, 0])
                    e_[0] += 1
                    e_[1] += int(has_book)
                detail = " ".join(f"{k[0]}-{k[1]}@{str(k[2])[-4:]}:{v[1]}/{v[0]}"
                                  for k, v in sorted(per.items()))
                log.info("books: %d tracked, %d levels, %d fresh | %s",
                         len(S.books), filled, fresh, detail)
        except Exception as e:
            log.warning("agg error: %s", e)
        await asyncio.sleep(AGG_INTERVAL)

# ---------------------------------------------------------------- main ----
async def main():
    log.info("symbols=%s tfs=%s data=%s", SYMBOLS, TFS, DATA_DIR)
    timeout = aiohttp.ClientTimeout(total=8)
    async with aiohttp.ClientSession(timeout=timeout, headers=UA) as session:
        tasks = [
            asyncio.create_task(rtds_task()),
            asyncio.create_task(binance_task()),
            asyncio.create_task(gamma_task(session)),
            asyncio.create_task(clob_books_task()),
            asyncio.create_task(aggregator()),
        ]
        await asyncio.gather(*tasks)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
