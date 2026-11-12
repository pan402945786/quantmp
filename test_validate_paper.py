import copy
import unittest
from dataclasses import replace
from datetime import date, timedelta

from research_system import PricePanel
from validate_paper import continuous_slice, scenarios, validate_case


class PaperValidationTests(unittest.TestCase):
    def setUp(self):
        self.dates = tuple((date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(70))
        prices = {on: {"AAA": 100 + i, "BBB": 200 + i} for i, on in enumerate(self.dates)}
        self.panel = PricePanel(self.dates, ("AAA", "BBB"), prices, "test", "test")
        self.base = {"initial_capital": 100000, "max_weight": 0.6}
        self.cases = scenarios(self.base, "2025-02-01T00:00:00Z", "2025-03-01T00:00:00Z")

    def test_warmup_cannot_silently_change_comparison_start(self):
        case = copy.deepcopy(self.cases[1])
        case["config"]["start_at"] = "2025-01-05T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "warmup"):
            validate_case(self.panel, case)

    def test_higher_costs_reduce_constant_price_equity_and_cash_stays_constant(self):
        panel = replace(self.panel, prices={on: {"AAA": 100, "BBB": 200} for on in self.dates})
        low, _, _, _ = validate_case(panel, self.cases[2])
        high, _, _, _ = validate_case(panel, self.cases[10])
        cash, _, _, _ = validate_case(panel, self.cases[-1])
        self.assertGreater(low["metrics"]["ending_equity"], high["metrics"]["ending_equity"])
        self.assertEqual(cash["metrics"]["ending_equity"], 100000)
        self.assertEqual(cash["metrics"]["total_fees"], 0)

    def test_segments_compound_to_continuous_return_without_resetting_positions(self):
        item, _, _, result = validate_case(self.panel, self.cases[1])
        first = continuous_slice(result, "2025-02-01T00:00:00Z", "2025-02-15T00:00:00Z")
        second = continuous_slice(result, "2025-02-15T00:00:00Z", "2025-03-02T00:00:00Z")
        self.assertEqual(first["ending_equity"], second["starting_equity"])
        self.assertAlmostEqual((1 + first["net_return"]) * (1 + second["net_return"]) - 1,
                               item["metrics"]["total_return"])

    def test_prices_after_end_do_not_change_decisions_or_equity(self):
        _, _, _, original = validate_case(self.panel, self.cases[1])
        changed = {on: {asset: value * (10 if on >= "2025-03-01" else 1)
                        for asset, value in row.items()} for on, row in self.panel.prices.items()}
        _, _, _, future_changed = validate_case(replace(self.panel, prices=changed), self.cases[1])
        self.assertEqual(original, future_changed)


if __name__ == "__main__":
    unittest.main()
