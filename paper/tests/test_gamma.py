from __future__ import annotations

import copy
import json
import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from typing import Any

from paper_bot.domain import FeeSchedule
from paper_bot.gamma import GammaClient, GammaValidationError, MarketDefinition, validate_market

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "gamma_market.json"
FIXTURE = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[0]
BTC_CURRENT = 1788250800
BTC_NEXT = BTC_CURRENT + 300
ETH_CURRENT = BTC_CURRENT
ETH_NEXT = ETH_CURRENT + 300


def clone_market() -> dict[str, Any]:
    return copy.deepcopy(FIXTURE)


def make_market(symbol: str, mkt_ts: int, *, token_prefix: str | None = None) -> dict[str, Any]:
    market = clone_market()
    prefix = token_prefix or symbol
    market["id"] = f"{symbol}-updown-5m-{mkt_ts}"
    market["conditionId"] = f"0x{symbol}updown5m{mkt_ts}"
    market["slug"] = market["id"]
    market["eventStartTime"] = f"2026-09-01T08:20:00Z" if mkt_ts == BTC_CURRENT else f"2026-09-01T08:25:00Z"
    market["endDate"] = f"2026-09-01T08:25:00Z" if mkt_ts == BTC_CURRENT else f"2026-09-01T08:30:00Z"
    market["resolutionSource"] = f"https://data.chain.link/streams/{symbol}-usd-twap-60s-streams"
    market["cryptoMarketConfigId"] = f"{symbol}-5m-twap-60"
    market["cryptoMarketConfig"] = {
        "id": f"{symbol}-5m-twap-60",
        "asset": symbol,
        "duration": "5m",
        "twapEnabled": True,
        "twapLookbackSeconds": 60,
    }
    market["outcomes"] = json.dumps(["Up", "Down"])
    market["clobTokenIds"] = json.dumps([
        f"{prefix}-up-{mkt_ts}-token",
        f"{prefix}-down-{mkt_ts}-token",
    ])
    return market


class GammaValidationTests(unittest.TestCase):
    def test_validate_market_accepts_official_fixture_and_ignores_listing_start_date(self):
        market = validate_market(clone_market(), "btc", BTC_CURRENT)
        self.assertIsInstance(market, MarketDefinition)
        self.assertEqual(market, MarketDefinition(
            symbol="btc",
            slug="btc-updown-5m-1788250800",
            market_id="btc-updown-5m-1788250800",
            mkt_ts=BTC_CURRENT,
            end_ts=BTC_NEXT,
            up_token_id="11111111111111111111111111111111111111111111111111111111111111111111",
            down_token_id="22222222222222222222222222222222222222222222222222222222222222222222",
            tick_size=Decimal("0.01"),
            min_order_shares=Decimal("5"),
            fee_schedule=FeeSchedule(Decimal("0.07"), Decimal("1")),
        ))
        self.assertEqual(market.fee_schedule, FeeSchedule(Decimal("0.07"), Decimal("1")))
        self.assertNotEqual(FIXTURE["startDate"], FIXTURE["eventStartTime"])
        with self.assertRaises(FrozenInstanceError):
            market.symbol = "eth"  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            market.fee_schedule.rate = Decimal("0.08")  # type: ignore[misc]

    def test_validate_market_rejects_noncanonical_rfc3339_timestamp_strings(self):
        cases = [
            ("space-event-start", {"eventStartTime": "2026-09-01 08:20:00Z"}),
            ("space-end-date", {"endDate": "2026-09-01 08:25:00Z"}),
            ("basic-event-start", {"eventStartTime": "20260901T082000Z"}),
            ("basic-end-date", {"endDate": "20260901T082500Z"}),
        ]
        for name, patch in cases:
            with self.subTest(name=name):
                payload = clone_market()
                payload.update(patch)
                with self.assertRaises(GammaValidationError):
                    validate_market(payload, "btc", BTC_CURRENT)

        offset_payload = clone_market()
        offset_payload["eventStartTime"] = "2026-09-01T09:20:00+01:00"
        offset_payload["endDate"] = "2026-09-01T09:25:00+01:00"
        market = validate_market(offset_payload, "btc", BTC_CURRENT)
        self.assertEqual(market.mkt_ts, BTC_CURRENT)
        self.assertEqual(market.end_ts, BTC_NEXT)

    def test_validate_market_rejects_slug_symbol_time_source_and_config_mismatches(self):
        cases: list[tuple[str, dict[str, Any], str, int]] = [
            ("slug", {"slug": "btc-updown-5m-9999999999"}, "btc", BTC_CURRENT),
            ("symbol", {}, "eth", BTC_CURRENT),
            ("time", {"eventStartTime": "2026-09-01T08:21:00Z"}, "btc", BTC_CURRENT),
            ("end-date", {"endDate": "2026-09-01T08:26:00Z"}, "btc", BTC_CURRENT),
            ("resolution", {"resolutionSource": "https://data.chain.link/streams/eth-usd-twap-60s-streams"}, "btc", BTC_CURRENT),
            ("config-id", {"cryptoMarketConfigId": "btc-5m-twap-30"}, "btc", BTC_CURRENT),
            ("config-asset", {"cryptoMarketConfig": {"id": "btc-5m-twap-60", "asset": "eth", "duration": "5m", "twapEnabled": True, "twapLookbackSeconds": 60}}, "btc", BTC_CURRENT),
            ("config-duration", {"cryptoMarketConfig": {"id": "btc-5m-twap-60", "asset": "btc", "duration": "15m", "twapEnabled": True, "twapLookbackSeconds": 60}}, "btc", BTC_CURRENT),
            ("config-enabled", {"cryptoMarketConfig": {"id": "btc-5m-twap-60", "asset": "btc", "duration": "5m", "twapEnabled": False, "twapLookbackSeconds": 60}}, "btc", BTC_CURRENT),
            ("config-lookback", {"cryptoMarketConfig": {"id": "btc-5m-twap-60", "asset": "btc", "duration": "5m", "twapEnabled": True, "twapLookbackSeconds": 30}}, "btc", BTC_CURRENT),
        ]
        for name, patch, symbol, mkt_ts in cases:
            with self.subTest(name=name):
                market = clone_market()
                market.update(patch)
                if "cryptoMarketConfig" in patch:
                    market["cryptoMarketConfigId"] = patch["cryptoMarketConfig"]["id"]
                with self.assertRaises(GammaValidationError):
                    validate_market(market, symbol, mkt_ts)

    def test_validate_market_rejects_interval_token_and_order_book_and_numeric_cases(self):
        cases: list[tuple[str, dict[str, Any]]] = [
            ("interval-short", {"endDate": "2026-09-01T08:24:00Z"}),
            ("interval-long", {"endDate": "2026-09-01T08:30:00Z"}),
            ("bad-outcomes-json", {"outcomes": "not-json"}),
            ("bad-outcomes-type", {"outcomes": json.dumps(["Up", 1])}),
            ("extra-outcomes", {"outcomes": json.dumps(["Up", "Down", "Sideways"])}),
            ("wrong-outcomes", {"outcomes": json.dumps(["YES", "NO"])}),
            ("bad-clob-json", {"clobTokenIds": "not-json"}),
            ("bad-clob-type", {"clobTokenIds": json.dumps(["one", 2])}),
            ("duplicate-token-ids", {"clobTokenIds": json.dumps(["dup", "dup"])}),
            ("empty-token-id", {"clobTokenIds": json.dumps(["", "down"])}),
            ("disabled-book", {"enableOrderBook": False}),
            ("zero-tick", {"orderPriceMinTickSize": 0}),
            ("bad-tick", {"orderPriceMinTickSize": 0.02}),
            ("nan-tick", {"orderPriceMinTickSize": Decimal("NaN")}),
            ("zero-min-size", {"orderMinSize": 0}),
            ("negative-min-size", {"orderMinSize": -1}),
            ("nan-min-size", {"orderMinSize": Decimal("NaN")}),
            ("non-bool-fees-enabled", {"feesEnabled": 1}),
            ("legacy-tick-alias-only", {"minimumTickSize": 0.01}),
            ("legacy-min-size-alias-only", {"minimumOrderSize": 5}),
        ]
        for name, patch in cases:
            with self.subTest(name=name):
                market = clone_market()
                market.update(patch)
                if name == "wrong-outcomes":
                    market["clobTokenIds"] = json.dumps(["a", "b"])
                if name == "legacy-tick-alias-only":
                    market.pop("orderPriceMinTickSize", None)
                if name == "legacy-min-size-alias-only":
                    market.pop("orderMinSize", None)
                with self.assertRaises(GammaValidationError):
                    validate_market(market, "btc", BTC_CURRENT)

    def test_validate_market_handles_fees_enabled_and_disabled(self):
        disabled = clone_market()
        disabled["feesEnabled"] = False
        disabled["feeSchedule"] = {"rate": -99, "exponent": "NaN", "takerOnly": False}
        market = validate_market(disabled, "btc", BTC_CURRENT)
        self.assertEqual(market.fee_schedule, FeeSchedule(Decimal("0"), Decimal("1")))

        missing = clone_market()
        missing.pop("feeSchedule")
        with self.assertRaises(GammaValidationError):
            validate_market(missing, "btc", BTC_CURRENT)

        bad = clone_market()
        bad["feeSchedule"] = {"rate": -0.1, "exponent": 1}
        with self.assertRaises(GammaValidationError):
            validate_market(bad, "btc", BTC_CURRENT)

    def test_validate_market_rejects_string_typed_numeric_fields(self):
        cases = [
            ("secondsDelay", {"secondsDelay": "0"}),
            ("orderPriceMinTickSize", {"orderPriceMinTickSize": "0.01"}),
            ("orderMinSize", {"orderMinSize": "5"}),
            ("feeSchedule.rate", {"feeSchedule": {"rate": "0.07", "exponent": 1, "takerOnly": True, "rebateRate": 0.2}}),
            ("feeSchedule.exponent", {"feeSchedule": {"rate": 0.07, "exponent": "1", "takerOnly": True, "rebateRate": 0.2}}),
            ("cryptoMarketConfig.twapLookbackSeconds", {"cryptoMarketConfig": {"id": "btc-5m-twap-60", "asset": "btc", "duration": "5m", "twapEnabled": True, "twapLookbackSeconds": "60"}}),
        ]
        for name, patch in cases:
            with self.subTest(name=name):
                payload = clone_market()
                payload.update(patch)
                if "cryptoMarketConfig" in patch:
                    payload["cryptoMarketConfigId"] = patch["cryptoMarketConfig"]["id"]
                with self.assertRaises(GammaValidationError):
                    validate_market(payload, "btc", BTC_CURRENT)

        accepted = clone_market()
        accepted.pop("secondsDelay", None)
        validated = validate_market(accepted, "btc", BTC_CURRENT)
        self.assertEqual(validated.fee_schedule, FeeSchedule(Decimal("0.07"), Decimal("1")))
        for value in (0, 0.0, Decimal("0")):
            with self.subTest(value=value):
                payload = clone_market()
                payload["secondsDelay"] = value
                validated = validate_market(payload, "btc", BTC_CURRENT)
                self.assertEqual(validated.fee_schedule, FeeSchedule(Decimal("0.07"), Decimal("1")))

    def test_validate_market_rejects_missing_distinct_token_and_wrong_order_cases(self):
        cases = [
            ("missing-outcome", {"outcomes": json.dumps(["Up"])}),
            ("missing-token", {"clobTokenIds": json.dumps(["token-only"])}),
            ("same-token", {"clobTokenIds": json.dumps(["same", "same"])}),
            ("reversed-order", {"outcomes": json.dumps(["Down", "Up"]), "clobTokenIds": json.dumps(["down-token", "up-token"])}),
        ]
        for name, patch in cases:
            with self.subTest(name=name):
                payload = clone_market()
                payload.update(patch)
                if name == "reversed-order":
                    market = validate_market(payload, "btc", BTC_CURRENT)
                    self.assertEqual(market.up_token_id, "up-token")
                    self.assertEqual(market.down_token_id, "down-token")
                    continue
                with self.assertRaises(GammaValidationError):
                    validate_market(payload, "btc", BTC_CURRENT)


class GammaClientTests(unittest.TestCase):
    def test_get_market_by_id_and_definition_use_exact_identity(self):
        calls: list[tuple[str, dict[str, str]]] = []
        payload = clone_market()

        async def get_json(url: str, params: dict[str, str]) -> Any:
            calls.append((url, params.copy()))
            return payload

        client = GammaClient("https://gamma-api.polymarket.com", get_json)
        self.assertEqual(self._run(client.get_market_by_id(payload["id"])), payload)
        definition = self._run(client.get_market_definition_by_id(
            payload["id"], ("btc", "eth", "sol"), BTC_CURRENT,
        ))
        self.assertEqual((definition.market_id, definition.symbol), (payload["id"], "btc"))
        self.assertEqual(calls, [
            (f"https://gamma-api.polymarket.com/markets/{payload['id']}", {}),
            (f"https://gamma-api.polymarket.com/markets/{payload['id']}", {}),
        ])

        payload["id"] = "different"
        with self.assertRaises(GammaValidationError):
            self._run(client.get_market_by_id("expected"))
        with self.assertRaises(GammaValidationError):
            self._run(client.get_market_definition_by_id(
                "different", ("eth", "sol"), BTC_CURRENT,
            ))

    def test_get_market_by_slug_enforces_official_list_response_shape_and_none_behavior(self):
        calls: list[tuple[str, dict[str, str]]] = []

        async def get_json(url: str, params: dict[str, str]) -> Any:
            calls.append((url, params.copy()))
            if params["slug"] == "btc-updown-5m-1788250800":
                return []
            if params["slug"] == "btc-updown-5m-1788251100":
                return [clone_market()]
            if params["slug"] == "btc-updown-5m-1788251400":
                return [clone_market(), clone_market()]
            if params["slug"] == "btc-updown-5m-1788251700":
                return [123]
            return {}

        client = GammaClient("https://gamma-api.polymarket.com", get_json)

        self.assertIsNone(self._run(client.get_market_by_slug("btc-updown-5m-1788250800")))
        self.assertEqual(self._run(client.get_market_by_slug("btc-updown-5m-1788251100")), clone_market())
        with self.assertRaises(GammaValidationError):
            self._run(client.get_market_by_slug("btc-updown-5m-1788251400"))
        with self.assertRaises(GammaValidationError):
            self._run(client.get_market_by_slug("btc-updown-5m-1788251700"))
        with self.assertRaises(GammaValidationError):
            self._run(client.get_market_by_slug("btc-updown-5m-1788252000"))
        self.assertEqual(
            self._run(client.get_market_by_slug(
                "btc-updown-5m-1788251100", include_closed=True,
            )),
            clone_market(),
        )
        with self.assertRaises(GammaValidationError):
            self._run(client.get_market_by_slug(
                "btc-updown-5m-1788251100", include_closed=1,  # type: ignore[arg-type]
            ))

        self.assertEqual(
            calls,
            [
                ("https://gamma-api.polymarket.com/markets", {"slug": "btc-updown-5m-1788250800"}),
                ("https://gamma-api.polymarket.com/markets", {"slug": "btc-updown-5m-1788251100"}),
                ("https://gamma-api.polymarket.com/markets", {"slug": "btc-updown-5m-1788251400"}),
                ("https://gamma-api.polymarket.com/markets", {"slug": "btc-updown-5m-1788251700"}),
                ("https://gamma-api.polymarket.com/markets", {"slug": "btc-updown-5m-1788252000"}),
                ("https://gamma-api.polymarket.com/markets", {
                    "slug": "btc-updown-5m-1788251100", "closed": "true",
                }),
            ],
        )
        self.assertFalse(hasattr(client, "request"))

    def test_discover_current_and_next_requests_current_then_next_for_each_symbol_and_returns_immutable_tuple(self):
        calls: list[tuple[str, dict[str, str]]] = []
        payloads = {
            "btc-updown-5m-1788250800": [make_market("btc", BTC_CURRENT)],
            "btc-updown-5m-1788251100": [make_market("btc", BTC_NEXT)],
            "eth-updown-5m-1788250800": [make_market("eth", ETH_CURRENT, token_prefix="eth")],
            "eth-updown-5m-1788251100": [make_market("eth", ETH_NEXT, token_prefix="eth")],
        }

        async def get_json(url: str, params: dict[str, str]) -> Any:
            calls.append((url, params.copy()))
            return payloads[params["slug"]]

        client = GammaClient("https://gamma-api.polymarket.com", get_json)
        result = self._run(client.discover_current_and_next(("btc", "eth"), 1788250899))

        self.assertIsInstance(result, tuple)
        self.assertEqual(
            [item.slug for item in result],
            [
                "btc-updown-5m-1788250800",
                "btc-updown-5m-1788251100",
                "eth-updown-5m-1788250800",
                "eth-updown-5m-1788251100",
            ],
        )
        self.assertEqual(calls, [
            ("https://gamma-api.polymarket.com/markets", {"slug": "btc-updown-5m-1788250800"}),
            ("https://gamma-api.polymarket.com/markets", {"slug": "btc-updown-5m-1788251100"}),
            ("https://gamma-api.polymarket.com/markets", {"slug": "eth-updown-5m-1788250800"}),
            ("https://gamma-api.polymarket.com/markets", {"slug": "eth-updown-5m-1788251100"}),
        ])
        with self.assertRaises(AttributeError):
            result.append(None)  # type: ignore[attr-defined]

        calls.clear()
        closed_result = self._run(client.discover_current_and_next(
            ("btc",), 1788250899, include_closed=True,
        ))
        self.assertEqual(len(closed_result), 2)
        self.assertEqual(calls, [
            ("https://gamma-api.polymarket.com/markets", {
                "slug": "btc-updown-5m-1788250800", "closed": "true",
            }),
            ("https://gamma-api.polymarket.com/markets", {
                "slug": "btc-updown-5m-1788251100", "closed": "true",
            }),
        ])

    def test_discover_current_and_next_skips_none_and_fails_closed_on_mismatch(self):
        calls: list[tuple[str, dict[str, str]]] = []
        payloads = {
            "btc-updown-5m-1788250800": [make_market("btc", BTC_CURRENT)],
            "btc-updown-5m-1788251100": [],
        }

        async def get_json(url: str, params: dict[str, str]) -> Any:
            calls.append((url, params.copy()))
            return payloads[params["slug"]]

        client = GammaClient("https://gamma-api.polymarket.com", get_json)
        result = self._run(client.discover_current_and_next(("btc",), 1788250899))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].mkt_ts, BTC_CURRENT)
        self.assertEqual(calls, [
            ("https://gamma-api.polymarket.com/markets", {"slug": "btc-updown-5m-1788250800"}),
            ("https://gamma-api.polymarket.com/markets", {"slug": "btc-updown-5m-1788251100"}),
        ])

        bad_payloads = {
            "btc-updown-5m-1788250800": [make_market("eth", BTC_CURRENT, token_prefix="eth")],
            "btc-updown-5m-1788251100": [],
        }

        async def bad_get_json(url: str, params: dict[str, str]) -> Any:
            return bad_payloads[params["slug"]]

        with self.assertRaises(GammaValidationError):
            self._run(GammaClient("https://gamma-api.polymarket.com", bad_get_json).discover_current_and_next(("btc",), 1788250899))

    def test_discover_current_and_next_rejects_invalid_now(self):
        async def get_json(url: str, params: dict[str, str]) -> Any:
            raise AssertionError("should not be called")

        client = GammaClient("https://gamma-api.polymarket.com", get_json)
        with self.assertRaises(GammaValidationError):
            self._run(client.discover_current_and_next(("btc",), -1))

    def _run(self, awaitable: Any) -> Any:
        try:
            import asyncio

            return asyncio.run(awaitable)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(awaitable)
            finally:
                loop.close()


if __name__ == "__main__":
    unittest.main()
