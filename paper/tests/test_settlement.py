from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError

from paper_bot.settlement import OfficialSettlement, parse_official_settlement


class SettlementTests(unittest.TestCase):
    def payload(self, **changes):
        value = {
            "slug": "btc-updown-5m-1000",
            "closed": True,
            "outcomes": '["Up","Down"]',
            "outcomePrices": '["1","0"]',
            "closedTime": "2026-09-01T08:25:00Z",
        }
        value.update(changes)
        return value

    def test_accepts_documented_closed_gamma_shape_without_nonstandard_resolved_field(self):
        result = parse_official_settlement(self.payload(), "btc-updown-5m-1000")
        self.assertEqual(result, OfficialSettlement("UP", 1788251100))
        down = parse_official_settlement(
            self.payload(outcomes='["Down","Up"]', outcomePrices='["1","0"]'),
            "btc-updown-5m-1000",
        )
        self.assertEqual(down.winner, "DOWN")

    def test_provisional_or_nonunique_prices_are_never_final(self):
        for prices in (
            '["0.9995","0.0005"]', '["1","1"]', '["0","0"]',
            '["0.5","0.5"]', '["NaN","0"]',
        ):
            with self.subTest(prices=prices):
                self.assertIsNone(
                    parse_official_settlement(
                        self.payload(outcomePrices=prices), "btc-updown-5m-1000"
                    )
                )

    def test_requires_exact_slug_closed_state_and_optional_resolved_truth(self):
        cases = (
            (self.payload(slug="other"), "btc-updown-5m-1000"),
            (self.payload(closed=False), "btc-updown-5m-1000"),
            (self.payload(closed=1), "btc-updown-5m-1000"),
            (self.payload(resolved=False), "btc-updown-5m-1000"),
            (self.payload(resolved=None), "btc-updown-5m-1000"),
            (self.payload(), ""),
        )
        for payload, slug in cases:
            with self.subTest(payload=payload, slug=slug):
                self.assertIsNone(parse_official_settlement(payload, slug))
        self.assertIsNotNone(
            parse_official_settlement(self.payload(resolved=True), "btc-updown-5m-1000")
        )

    def test_requires_official_json_string_pairs_and_exact_outcomes(self):
        cases = (
            {"outcomes": ["Up", "Down"]},
            {"outcomePrices": ["1", "0"]},
            {"outcomes": '["UP","DOWN"]'},
            {"outcomes": '["Up","Up"]'},
            {"outcomes": '["Up"]'},
            {"outcomePrices": '[1,0]'},
            {"outcomePrices": "not-json"},
            {"outcomes": None},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertIsNone(
                    parse_official_settlement(self.payload(**changes), "btc-updown-5m-1000")
                )

    def test_resolution_timestamp_is_optional_but_malformed_values_fail_closed(self):
        without_time = self.payload()
        without_time.pop("closedTime")
        self.assertEqual(
            parse_official_settlement(without_time, "btc-updown-5m-1000"),
            OfficialSettlement("UP", None),
        )
        self.assertEqual(
            parse_official_settlement(
                self.payload(resolvedAt=1234), "btc-updown-5m-1000"
            ).resolved_at,
            1234,
        )
        for value in (-1, True, 1.5, "bad-time", "2026-09-01T08:25:00"):
            with self.subTest(value=value):
                self.assertIsNone(
                    parse_official_settlement(
                        self.payload(resolvedAt=value), "btc-updown-5m-1000"
                    )
                )

    def test_settlement_record_is_validated_and_immutable(self):
        settlement = OfficialSettlement("UP", 1)
        with self.assertRaises(FrozenInstanceError):
            settlement.winner = "DOWN"
        for winner, resolved_at in (("Up", 1), ("TIE", 1), ("UP", -1), ("UP", True)):
            with self.subTest(winner=winner, resolved_at=resolved_at), self.assertRaises(ValueError):
                OfficialSettlement(winner, resolved_at)


if __name__ == "__main__":
    unittest.main()
