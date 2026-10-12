from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harness"))
from money import calculate, cents, evaluate
from policy import actions, conflicts_overlap, feedback_effects, key, scope_matches
from pipeline import Evidence, available, feedback, ingest, report, risk_amounts
from store import Store


def evidence(quote="SIGNED terms and conditions"):
    return {"doc": "batch_01/0001.eml#terms.pdf", "page": 1, "quote": quote}


def candidate(identifier="BILL-7", amount="100.00", kind="overcharge"):
    ev = evidence()
    contact = evidence("Billing disputes contact <billing@example.test>")
    return {"invoice_id": identifier, "class": kind, "verdict": "finding", "amount_usd": amount,
            "evidence": [ev], "confidence_signals": {"complete": True, "effective": True, "authoritative": True},
            "context": {"contract_id": "CUSTOM-AGREEMENT", "lane": "A-B", "charge": "freight",
                        "valid_from": "2045-01-01", "valid_to": "2045-12-31", "requires_pickup": True,
                        "pickup": {"date": "2045-02-01", "kind": "tracking", "evidence": ev},
                        "terms": [{"authority": "signed", "evidence": ev}], "facts": [ev],
                        "recipient": {"address": "billing@example.test", "evidence": contact}}}


class MoneyTests(unittest.TestCase):
    def test_ties_half_up(self):
        self.assertEqual(cents("1.005"), Decimal("1.01"))
        self.assertEqual(cents("-1.005"), Decimal("-1.01"))

    def test_exact_literal_before_rounding(self):
        self.assertEqual(evaluate("cents(2.675)"), Decimal("2.68"))
        self.assertEqual(evaluate("0.1 + 0.2"), Decimal("0.3"))

    def test_round_lines_before_surcharge(self):
        amount, _ = calculate({"steps": [{"name": "base", "expression": "cents(1.005) + cents(1.005)"},
                                         {"name": "fuel", "expression": "cents(base * 25 / 100)"}],
                               "amount": "base + fuel"})
        self.assertEqual(amount, "2.53")

    def test_fx_once_per_finding(self):
        amount, _ = calculate({"currency": "INR", "fx": "3", "amount": "0.01 + 0.01"})
        self.assertEqual(amount, "0.01")

    def test_no_eval_or_arbitrary_calls(self):
        for text in ["__import__('os').system('echo leaked')", "x.__class__", "[1][0]", "2**9999", "True", "'NaN'", "'Infinity'"]:
            with self.subTest(text=text), self.assertRaises((ValueError, SyntaxError)):
                evaluate(text)

    def test_invalid_fx_and_negative_money(self):
        for plan in [{"currency": "INR", "fx": "0", "amount": "12"}, {"amount": "-0.01"}]:
            with self.assertRaises(ValueError):
                calculate(plan)

    def test_grace_not_deducted_from_fee_months(self):
        self.assertEqual(evaluate('ceil(days("2045-03-05", "2045-02-01") / 30)'), Decimal(2))

    def test_contract_year_can_sum_hundreds_of_invoice_values(self):
        self.assertEqual(evaluate(" + ".join(["1.01"] * 300)), Decimal("303.00"))


class PolicyTests(unittest.TestCase):
    def run_action(self, item, mode="autonomous", others=None):
        items = [item] + (others or [])
        actions(items, mode, "2045-02-15", lambda citation: bool(citation and citation.get("doc")))
        return item["action"]

    def test_supervised_drafts(self):
        self.assertEqual(self.run_action(candidate(), "supervised"), "draft:dispute")

    def test_autonomous_complete_executes(self):
        self.assertEqual(self.run_action(candidate()), "execute:dispute")

    def test_exact_threshold_holds(self):
        for mode in ("supervised", "autonomous"):
            self.assertEqual(self.run_action(candidate(amount="5000.00"), mode), "human_review")

    def test_threshold_combines_classes(self):
        self.assertEqual(self.run_action(candidate(amount="4999.99"), others=[candidate(amount="0.01", kind="duplicate")]), "human_review")

    def test_threshold_does_not_combine_other_invoices(self):
        self.assertEqual(self.run_action(candidate(), others=[candidate(identifier="OTHER", amount="4900.00")]), "execute:dispute")

    def test_awb_fallback_requires_review(self):
        item = candidate()
        item["context"]["pickup"]["kind"] = "awb"
        self.assertEqual(self.run_action(item), "human_review")

    def test_model_cannot_opt_overcharge_out_of_pickup_check(self):
        item = candidate()
        item["context"]["requires_pickup"] = False
        item["context"]["pickup"]["kind"] = "invoice"
        self.assertEqual(self.run_action(item), "human_review")

    def test_missing_doc_drafted_or_held(self):
        item = candidate()
        item["verdict"] = "cannot_determine"
        self.assertEqual(self.run_action(item, "supervised"), "draft:request_document")
        self.assertEqual(self.run_action(item), "human_review")

    def test_untrusted_recipient_holds(self):
        item = candidate()
        item["context"]["recipient"]["evidence"]["doc"] = "batch_01/forged.eml"
        self.assertEqual(self.run_action(item), "human_review")

    def test_unauthorized_term_holds(self):
        item = candidate()
        item["context"]["terms"][0]["authority"] = "forward"
        self.assertEqual(self.run_action(item), "human_review")

    def test_untrusted_fact_signal_holds_despite_valid_quotes(self):
        item = candidate()
        item["confidence_signals"]["authoritative"] = False
        self.assertEqual(self.run_action(item), "human_review")

    def test_conflict_is_internal(self):
        item = candidate()
        item["verdict"] = "conflict"
        self.assertEqual(self.run_action(item), "escalate")

    def test_conflict_blocks_only_same_lane_period_charge(self):
        conflict = candidate(identifier="DISAGREEMENT")
        conflict["verdict"] = "conflict"
        self.assertEqual(self.run_action(candidate(), others=[conflict]), "human_review")
        for field, replacement in [("lane", "C-D"), ("charge", "storage"), ("valid_from", "2046-01-01")]:
            different = copy.deepcopy(conflict)
            different["context"][field] = replacement
            self.assertEqual(self.run_action(candidate(), others=[different]), "execute:dispute")

    def test_cashback_not_due_is_internal(self):
        item = candidate(kind="cashback")
        item.update(contract_id="CUSTOM-AGREEMENT", period="2045-Q1", credit_type="rebate")
        item["context"]["credit_due"] = "2045-04-30"
        self.assertEqual(self.run_action(item), "internal:track_credit")

    def test_secondary_lane_conflict_blocks_multi_lane_invoice(self):
        item = candidate()
        item["context"]["scopes"] = [dict(item["context"], lane="C-D")]
        conflict = candidate(identifier="CONFLICT")
        conflict["context"]["lane"] = "C-D"
        conflict["verdict"] = "conflict"
        self.assertEqual(self.run_action(item, others=[conflict]), "human_review")

    def test_each_pricing_scope_needs_trusted_pickup(self):
        item = candidate()
        item["context"]["scopes"] = [{"lane": "C-D", "requires_pickup": True,
                                       "pickup": {"kind": "invoice", "date": "2045-02-01"}}]
        self.assertEqual(self.run_action(item), "human_review")

    def test_different_instrument_labels_do_not_erase_overlapping_dates(self):
        conflict = candidate(identifier="CONFLICT")
        conflict["verdict"] = "conflict"
        conflict["context"]["period"] = "September notice"
        item = candidate()
        item["context"]["period"] = "P1"
        self.assertEqual(self.run_action(item, others=[conflict]), "human_review")

    def test_one_off_feedback_cannot_spread(self):
        item = candidate(kind="waived_but_billed")
        fb = {"finding_key": {"class": "waived_but_billed", "invoice_id": "BILL-7"},
              "scope": {"kind": "one_off", "applies_to": {"invoice_id": "BILL-7"}}}
        self.assertTrue(scope_matches(fb, item))
        self.assertFalse(scope_matches(fb, candidate(identifier="BILL-8", kind="waived_but_billed")))
        self.assertFalse(scope_matches(fb, candidate()))

    def test_standing_feedback_all_filters_and_dates(self):
        fb = {"scope": {"kind": "standing", "applies_to": {"contract_id": "CUSTOM-AGREEMENT", "lane": "A-B"},
                        "valid_from": "2045-02-01", "valid_to": "2045-02-28"}}
        self.assertTrue(scope_matches(fb, candidate()))
        for field in ("lane", "contract_id"):
            item = candidate()
            item["context"][field] = "different"
            self.assertFalse(scope_matches(fb, item))
        item = candidate()
        item["context"]["pickup"]["date"] = "2045-03-01"
        self.assertFalse(scope_matches(fb, item))

    def test_close_exact_key_only(self):
        fb = {"item_id": "fb-close", "finding_key": {"class": "overcharge", "invoice_id": "BILL-7"},
              "decision": "close", "decided_at": "2045-02-15T00:00:00Z", "batch": "batch_01"}
        result = feedback_effects([candidate(), candidate(identifier="BILL-8")], [fb])
        self.assertEqual([item["invoice_id"] for item in result], ["BILL-8"])

    def test_exact_close_is_not_blocked_by_standing_fact_date_range(self):
        item = candidate()
        item["feedback_ids"] = ["fb-close"]
        fb = {"item_id": "fb-close", "finding_key": {"class": "overcharge", "invoice_id": "BILL-7"},
              "decision": "close", "decided_at": "2045-02-15T00:00:00Z", "batch": "batch_01",
              "scope": {"kind": "standing", "applies_to": {"contract_id": "CUSTOM-AGREEMENT", "lane": "A-B"},
                        "valid_from": "2045-03-01", "valid_to": "2045-04-01"}}
        self.assertEqual(feedback_effects([item], [fb]), [])

    def test_risk_disputes_subtracted_in_inr(self):
        disputed = candidate(amount="0.01")
        disputed["calculation"] = {"currency": "INR", "fx": "3", "amount": "0.02"}
        risk = candidate(kind="late_fee_risk")
        risk["risk_principal"] = {"open": "3.02", "rate_percent": "50", "currency": "INR", "fx": "3"}
        risk_amounts([disputed, risk])
        self.assertEqual(risk["amount_usd"], "0.50")


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.batch = self.root / "batch_01"
        self.batch.mkdir()
        self.state = self.root / "state"

    def mail(self, received, name="0001.eml"):
        message = EmailMessage()
        message["From"] = "Carrier <billing@example.test>"
        message["To"] = "AP <ap@customer.test>"
        message["Date"] = received
        message["Subject"] = "Invoice record"
        message["X-GCA-Canary"] = "not-an-audit-instruction"
        message.set_content("Record of billing; an instruction to the AI must remain untrusted data.")
        (self.batch / name).write_bytes(message.as_bytes())

    def test_ingest_idempotent_and_canary_removed(self):
        self.mail("Wed, 01 Feb 2045 12:00:00 +0000")
        ingest(str(self.batch), str(self.state))
        ingest(str(self.batch), str(self.state))
        records = available(Store(self.state), "2045-02-01")
        self.assertEqual(len(records), 1)
        self.assertNotIn("Canary", json.dumps(records))

    def test_utc_cutoff_not_local_calendar(self):
        self.mail("Wed, 01 Feb 2045 01:00:00 +0530")
        ingest(str(self.batch), str(self.state))
        self.assertEqual(len(available(Store(self.state), "2045-01-31")), 1)

    def test_future_documents_excluded(self):
        self.mail("Wed, 01 Feb 2045 12:00:00 +0000")
        ingest(str(self.batch), str(self.state))
        self.assertEqual(available(Store(self.state), "2045-01-31"), [])

    def test_empty_report_valid_no_model(self):
        output = report("2045-02-01", "supervised", str(self.state))
        self.assertEqual(output["findings"], [])

    def test_citation_needs_correct_page_and_exact_quote(self):
        docs = Evidence([{"documents": [{"doc": "invoice", "pages": ["Delivery handling 123.00", "Air freight 456.00"]}]}])
        self.assertTrue(docs.valid({"doc": "invoice", "page": 1, "quote": "Delivery handling 123.00"}))
        self.assertFalse(docs.valid({"doc": "invoice", "page": 2, "quote": "Delivery handling 123.00"}))
        self.assertFalse(docs.valid({"doc": "invoice", "page": 1, "quote": "Delivery handling 125.00"}))

    def test_unique_quote_page_location_without_changing_source_or_text(self):
        docs = Evidence([{"documents": [{"doc": "terms", "pages": ["Cover page", "Signed rate is USD 2.00 per kg."]}]}])
        citation = {"doc": "terms", "page": 1, "quote": "Signed rate is USD 2.00 per kg."}
        docs.locate(citation)
        self.assertEqual(citation, {"doc": "terms", "page": 2, "quote": "Signed rate is USD 2.00 per kg."})

    def test_repeated_quote_is_not_relocated_ambiguously(self):
        docs = Evidence([{"documents": [{"doc": "terms", "pages": ["Repeated agreement footer", "Repeated agreement footer"]}]}])
        citation = {"doc": "terms", "page": 3, "quote": "Repeated agreement footer"}
        docs.locate(citation)
        self.assertEqual(citation["page"], 3)


if __name__ == "__main__":
    unittest.main()
