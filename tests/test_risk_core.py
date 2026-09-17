#!/usr/bin/env python3
"""Unit tests for the deterministic risk-core primitives."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest

from marketflow.mcp import risk_core as r


class RiskCoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = r.AppendOnlyStore(self.tmp.name)
        self.now = 1_800_000_000.0

    def tearDown(self):
        self.tmp.cleanup()

    def meta(self, **overrides):
        row = {
            "id": "42", "conditionId": "0x" + "ab" * 32,
            "slug": "will-the-test-pass", "eventSlug": "test-event",
            "question": "Will the test pass?", "description": "This market resolves Yes if the test passes.",
            "outcomes": '["Yes","No"]', "outcomePrices": '["0.6","0.4"]',
            "clobTokenIds": '["123","456"]', "active": True, "closed": False,
            "endDate": r.utc_now(self.now + 10 * 86400),
            "resolutionSource": "official test record", "resolvedBy": "uma",
            "updatedAt": r.utc_now(self.now),
        }
        row.update(overrides)
        return row

    def position(self, **overrides):
        row = {
            "conditionId": "0x" + "ab" * 32, "asset": "123", "slug": "will-the-test-pass",
            "eventSlug": "test-event", "title": "Will the test pass?", "outcome": "Yes",
            "outcomeIndex": 0, "size": 100, "avgPrice": .5, "curPrice": .6,
            "initialValue": 50, "currentValue": 60, "cashPnl": 10,
            "realizedPnl": 0, "redeemable": False,
            "endDate": r.utc_now(self.now + 10 * 86400),
        }
        row.update(overrides)
        return row

    def book(self, **overrides):
        row = {"timestamp": str(int(self.now * 1000)),
               "bids": [{"price": ".59", "size": "70"}, {"price": ".57", "size": "50"}]}
        row.update(overrides)
        return row

    def test_liquidation_and_insufficient_depth(self):
        rows = r.liquidation_scenarios(100, .60, self.book()["bids"])
        self.assertFalse(rows[-1]["insufficient_depth"])
        self.assertAlmostEqual(rows[-1]["estimated_average_price"], .584, places=3)
        thin = r.liquidation_scenarios(100, .60, [{"price": ".59", "size": "20"}])
        self.assertTrue(thin[-1]["insufficient_depth"])
        self.assertEqual(thin[-1]["executable_size"], 20)

    def test_unknown_is_fail_closed(self):
        components = [r.component("resolution_state", "low", ["open"], [], r.utc_now(self.now), "fresh"),
                      r.component("liquidity_exit", "unknown", ["no book"], [], r.utc_now(self.now), "unknown")]
        self.assertEqual(r.aggregate(components)[0], "unknown")

    def test_critical_precedes_unknown(self):
        components = [r.component("resolution_state", "critical", ["disputed"], [], r.utc_now(self.now), "fresh"),
                      r.component("liquidity_exit", "unknown", ["no book"], [], r.utc_now(self.now), "unknown")]
        self.assertEqual(r.aggregate(components)[0], "critical")

    def test_resolution_transitions(self):
        self.assertEqual(r.normalized_resolution_state(self.meta(), self.position())[0], "OPEN")
        self.assertEqual(r.normalized_resolution_state(
            self.meta(umaResolutionStatuses='["proposed"]'), self.position())[0], "DISPUTE_WINDOW")
        self.assertEqual(r.normalized_resolution_state(
            self.meta(umaResolutionStatuses='["disputed"]'), self.position())[0], "DISPUTED")
        self.assertEqual(r.normalized_resolution_state(
            self.meta(closed=True, active=False, outcomePrices='["1","0"]'),
            self.position(redeemable=True))[0], "REDEEMABLE")

    def test_rule_versions_are_append_only_and_diffed(self):
        first = self.store.observe_rules("c1", "rule A", r.utc_now(self.now))
        again = self.store.observe_rules("c1", "rule A", r.utc_now(self.now + 1))
        changed = self.store.observe_rules("c1", "rule B", r.utc_now(self.now + 2))
        self.assertIsNone(first["changed"])
        self.assertFalse(again["changed"])
        self.assertTrue(changed["changed"])
        self.assertNotEqual(first["version"], changed["version"])
        rule_file = next(os.path.join(self.tmp.name, "rules", x)
                         for x in os.listdir(os.path.join(self.tmp.name, "rules")))
        self.assertEqual(len(self.store._rows(rule_file)), 2)
        self.assertTrue(changed["diff"])

    def test_contract_risk_stale_book(self):
        stale_book = self.book(timestamp=str(int((self.now - 601) * 1000)))
        result = r.contract_risk(self.position(), self.meta(), stale_book,
                                 store=self.store, now=self.now)
        by_name = {c["component"]: c for c in result["risk_components"]}
        self.assertEqual(by_name["data_freshness"]["level"], "high")
        self.assertEqual(result["marketflow_contract_id"],
                         "marketflow:polymarket:" + self.meta()["conditionId"])

    def test_portfolio_concentration(self):
        a = r.contract_risk(self.position(), self.meta(), self.book(),
                            store=self.store, now=self.now)
        bmeta = self.meta(conditionId="0x" + "cd" * 32, eventSlug="event-b")
        b = r.contract_risk(self.position(conditionId=bmeta["conditionId"], currentValue=20,
                                          size=30), bmeta, self.book(),
                            store=self.store, now=self.now)
        portfolio = r.portfolio_rollup("0x" + "11" * 20, [], [a, b],
                                       now=self.now, store=self.store)
        self.assertEqual(portfolio["summary"]["total_current_exposure"], 80)
        concentration = {x["marketflow_contract_id"]: x["risk_components"][-1]
                         for x in portfolio["positions"]}
        self.assertEqual(concentration[a["marketflow_contract_id"]]["level"], "high")
        self.assertEqual(concentration[b["marketflow_contract_id"]]["level"], "low")

    def test_decisions_never_mutate(self):
        result = r.contract_risk(self.position(), self.meta(), self.book(),
                                 store=self.store, now=self.now)
        first = self.store.decision("0x" + "22" * 20, result)
        second = self.store.decision("0x" + "22" * 20, result)
        rows = self.store._rows(os.path.join(self.tmp.name, "decisions.jsonl"))
        self.assertEqual([x["decision_id"] for x in rows], [first, second])
        self.assertNotEqual(first, second)

    def test_webhook_signature_idempotency_and_delivery(self):
        config = {"endpoints": [{"id": "desk", "url": "https://example.test/risk",
                                  "secret": "secret", "enabled": True}]}
        with open(os.path.join(self.tmp.name, "webhooks.json"), "w", encoding="utf-8") as fh:
            json.dump(config, fh)
        event = self.store.emit("risk.level_changed", "c1", {"to": "high"}, r.utc_now(self.now))
        self.store.enqueue_webhooks(event)
        queued = self.store._rows(os.path.join(self.tmp.name, "webhook_deliveries.jsonl"))
        self.assertEqual(len(queued), 1)

        captured = {}

        class Response:
            status = 204
            def __enter__(self): return self
            def __exit__(self, *args): return False

        def opener(req, timeout):
            captured["delivery"] = req.get_header("X-marketflow-delivery")
            captured["signature"] = req.get_header("X-marketflow-signature")
            return Response()

        delivered = r.WebhookWorker(self.store, opener=opener).run_once(now=self.now)
        self.assertEqual(delivered, 1)
        self.assertEqual(captured["delivery"], queued[0]["delivery_id"])
        self.assertTrue(captured["signature"].startswith("v1="))
        self.assertEqual(r.WebhookWorker(self.store, opener=opener).run_once(now=self.now + 1), 0)

    def test_existing_api_contract_remains_present(self):
        from marketflow.mcp import server
        self.assertEqual(set(server.TOOLS),
                         {"search_markets", "market_snapshot", "resolution_risk", "whale_prints"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
