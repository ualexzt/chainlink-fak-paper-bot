from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path

from paper_bot.books import InvalidBook, OrderBook
from paper_bot.market_ws import (
    MarketDelta, MarketInvalidation, MarketSnapshot, MarketWsClient,
    parse_market_message,
)

FIXTURE = Path(__file__).parent / "fixtures" / "market_ws_replay.jsonl"


class FakeWebSocket:
    def __init__(self, incoming=()):
        self.incoming = tuple(incoming)
        self.sent = []
        self.closed = False

    async def send(self, value):
        self.sent.append(value)

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for value in self.incoming:
            await asyncio.sleep(0)
            if isinstance(value, BaseException):
                raise value
            yield value

    async def close(self):
        self.closed = True


class InteractiveWebSocket(FakeWebSocket):
    def __init__(self):
        super().__init__()
        self.queue = asyncio.Queue()

    async def send(self, value):
        await super().send(value)
        if value == "PING":
            await self.queue.put("PONG")

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()


class MarketParserTests(unittest.TestCase):
    def test_official_direct_and_camelcase_frames_replay(self):
        lines = FIXTURE.read_text().splitlines()
        first = parse_market_message(lines[0], sequence=1000)
        self.assertEqual(len(first), 1)
        self.assertIsInstance(first[0], MarketSnapshot)
        self.assertEqual((first[0].token_id, str(first[0].asks[0].price)), ("up-token", "0.80"))

        changes = parse_market_message(lines[1], sequence=2000)
        self.assertEqual([(item.side, item.price, item.shares) for item in changes], [
            ("ask", first[0].asks[0].price, first[0].asks[0].shares * 0),
            ("ask", type(first[0].asks[0].price)("0.81"), type(first[0].asks[0].shares)("7.5")),
        ])
        camel_snapshot = parse_market_message(lines[2], sequence=3000)[0]
        camel_delta = parse_market_message(lines[3], sequence=4000)[0]
        self.assertEqual((camel_snapshot.token_id, camel_delta.token_id, camel_delta.side),
                         ("down-token", "down-token", "bid"))

    def test_snapshot_and_deltas_integrate_with_generation_and_zero_removal(self):
        lines = FIXTURE.read_text().splitlines()
        book = OrderBook()
        snapshot = parse_market_message(lines[0], sequence=1000)[0]
        assert isinstance(snapshot, MarketSnapshot)
        self.assertEqual(book.apply_snapshot(
            snapshot.bids, snapshot.asks, snapshot.event_ts_ms, snapshot.sequence
        ), 1)
        for delta in parse_market_message(lines[1], sequence=2000):
            assert isinstance(delta, MarketDelta)
            self.assertTrue(book.apply_delta(
                delta.side, delta.price, delta.shares, delta.event_ts_ms, delta.sequence
            ))
        self.assertEqual([str(level.price) for level in book.executable_asks()], ["0.81"])

    def test_malformed_unknown_and_nonofficial_single_delta_fail_closed(self):
        cases = (
            "not-json", "PONG", "[]", "{}",
            '{"event_type":"last_trade_price","timestamp":"1","asset_id":"u"}',
            '{"event_type":"price_change","timestamp":"1","price_changes":[{"asset_id":"u","price":"NaN","size":"1","side":"BUY"}]}',
            '{"asset_id":"u","side":"ask","price":"0.5","size":"1","timestamp":"1"}',
        )
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertEqual(parse_market_message(raw, sequence=1), ())


class MarketClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_consumer_buffers_deltas_until_every_token_has_snapshot(self):
        lines = FIXTURE.read_text().splitlines()
        websocket = FakeWebSocket((lines[0], lines[1], lines[2]))
        queue = asyncio.Queue()
        with self.assertRaisesRegex(ConnectionError, "closed"):
            await MarketWsClient(lambda _url: None)._consume(
                websocket, ("up-token", "down-token"), queue, asyncio.Event()
            )
        events = [queue.get_nowait() for _ in range(queue.qsize())]
        self.assertEqual([type(item) for item in events],
                         [MarketSnapshot, MarketSnapshot, MarketDelta, MarketDelta])

    async def test_connection_sends_exact_public_subscription_and_ping(self):
        websocket = InteractiveWebSocket()

        async def connector(url):
            self.assertEqual(url, "wss://ws-subscriptions-clob.polymarket.com/ws/market")
            return websocket

        client = MarketWsClient(connector, ping_interval=0.001)
        task = asyncio.create_task(client._connection(("up-token", "down-token"), asyncio.Queue()))
        for _ in range(100):
            if "PING" in websocket.sent:
                break
            await asyncio.sleep(0.001)
        self.assertEqual(
            json.loads(websocket.sent[0]),
            {"assets_ids": ["up-token", "down-token"], "type": "market"},
        )
        self.assertIn("PING", websocket.sent)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(websocket.closed)

    async def test_run_invalidates_and_resubscribes_current_token_set(self):
        lines = FIXTURE.read_text().splitlines()
        sockets = [FakeWebSocket((lines[0],)), FakeWebSocket((lines[2],))]
        supplied = [("up-token",), ("down-token",)]
        calls = 0

        async def connector(_url):
            nonlocal calls
            if calls >= len(sockets):
                await asyncio.Future()
            websocket = sockets[calls]
            calls += 1
            return websocket

        def supplier():
            return supplied[min(calls, 1)]

        queue = asyncio.Queue()
        client = MarketWsClient(connector, reconnect_base=0)
        task = asyncio.create_task(client.run(supplier, queue))
        for _ in range(100):
            if calls >= 2 and queue.qsize() >= 4:
                break
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        events = [queue.get_nowait() for _ in range(queue.qsize())]
        self.assertEqual(
            [type(item) for item in events[:4]],
            [MarketSnapshot, MarketInvalidation, MarketSnapshot, MarketInvalidation],
        )
        invalidations = [item.token_ids for item in events if isinstance(item, MarketInvalidation)]
        self.assertEqual(invalidations[:2], [("up-token",), ("down-token",)])
        self.assertEqual(json.loads(sockets[0].sent[0])["assets_ids"], ["up-token"])
        self.assertEqual(json.loads(sockets[1].sent[0])["assets_ids"], ["down-token"])


if __name__ == "__main__":
    unittest.main()
