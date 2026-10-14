"""Fresh-process state semantics with scripted model proposals, never paid API calls."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "harness"))
from pipeline import feedback, ingest, report
from model import Model
from store import Store
from grader.validate import schema_errors


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.state = self.root / "state"
        self.batch = self.root / "batch_01"
        self.batch.mkdir()
        for name, body in [("contract", "Signed agreement GENERIC-1; base freight is USD 2.00 per kg."),
                           ("invoice", "Bill BILL-7 issued 2045-02-01: base freight USD 110.00 for 50 kg.")]:
            message = EmailMessage()
            message["From"] = "Morgan Hale <morgan@example.test>"
            message["To"] = "AP <ap@customer.test>"
            message["Date"] = "Wed, 01 Feb 2045 12:00:00 +0000"
            message["Subject"] = name
            message.set_content(body)
            (self.batch / f"{name}.eml").write_bytes(message.as_bytes())
        ingest(str(self.batch), str(self.state))
        self.verdict = "finding"
        self.calls = []
        self.bad_quote = False
        self.mock = patch.object(Model, "ask", side_effect=self.ask)
        self.mock.start()
        self.addCleanup(self.mock.stop)

    def ask(self, role, instructions, data, max_tokens=14000):
        self.calls.append(role)
        if role == "document_index":
            return {"records": [{"email": row["email"], "contract_ids": ["GENERIC-1"],
                       "invoice_ids": ["BILL-7"] if row["email"].endswith("invoice.eml") else [],
                       "billed_invoice_ids": ["BILL-7"] if row["email"].endswith("invoice.eml") else [],
                       "shipment_ids": [], "kinds": ["invoice"] if row["email"].endswith("invoice.eml") else ["agreement"],
                       "global": False} for row in data["emails"]]}
        ev = {"doc": "batch_01/invoice.eml", "page": 1, "quote": "base freight USD 110.00 for 50 kg."}
        clause = {"doc": "batch_01/contract.eml", "page": 1, "quote": "base freight is USD 2.00 per kg."}
        if self.bad_quote and role != "repair":
            ev["quote"] = "invented source quote is unacceptable"
        item = {"class": "overcharge", "invoice_id": "BILL-7", "verdict": self.verdict,
                "calculation": {"amount": "max(0, billed - cents(rate * weight))", "currency": "USD", "steps": [],
                    "inputs": [{"name": name, "value": value, "source_text": value, "evidence": source}
                               for name, value, source in [("billed", "110.00", ev), ("rate", "2.00", clause), ("weight", "50", ev)]]},
                "evidence": [ev, clause], "context": {"contract_id": "GENERIC-1", "requires_pickup": True,
                             "pickup": {"date": "2045-01-28", "kind": "invoice", "evidence": ev}},
                "confidence_signals": {"complete": True, "authoritative": True, "extraction": 1,
                                       "agreement": 1, "effective": True, "ambiguity": 0}}
        if any(doc["doc"] == "batch_02/credit.eml" for doc in data["documents"]):
            credit = {"doc": "batch_02/credit.eml", "page": 1, "quote": "Credit issued USD 1.00 against BILL-7 freight."}
            item["calculation"]["inputs"].append({"name": "credit", "value": "1.00", "source_text": "1.00", "evidence": credit})
            item["calculation"]["amount"] = "max(0, billed - cents(rate * weight) - credit)"
            item["evidence"].append(credit)
        items = [item] if "BILL-7" in data["target_invoice_ids"] else []
        if self.verdict == "cannot_determine":
            item["missing_doc"] = {"name": "rate annex", "holder": {"person": "Morgan Hale"}}
        return {"items": items, "coverage": [{"invoice_id": "BILL-7", "checked": ["overcharge"]}], "portfolio_checked": ["GENERIC-1"]}

    def test_proposals_go_through_actual_schema_money_and_actions(self):
        result = report("2045-02-15", "supervised", str(self.state))
        self.assertEqual(schema_errors(result), [])
        self.assertEqual(result["findings"][0]["amount_usd"], "10.00")
        self.assertEqual(result["findings"][0]["action"], "draft:dispute")
        self.assertIn("audit", self.calls)
        self.assertIn("review", self.calls)

    def test_mode_safety_is_independent_of_model_proposal(self):
        result = report("2045-02-15", "autonomous", str(self.state))
        self.assertEqual(result["findings"][0]["action"], "human_review")

    def test_optional_period_null_is_omitted_and_non_null_preserved(self):
        def ask_with_period(*args, **kwargs):
            response = self.ask(*args, **kwargs)
            for item in response.get("items", []):
                item["period"] = period
            return response

        with patch.object(Model, "ask", side_effect=ask_with_period):
            for verdict in ("finding", "no_finding"):
                self.verdict = verdict
                for period in (None, "P2"):
                    with self.subTest(verdict=verdict, period=period):
                        result = report("2045-02-15", "supervised", str(self.state))
                        self.assertEqual(schema_errors(result), [])
                        item = result["findings"][0]
                        self.assertEqual(item["verdict"], verdict)
                        self.assertEqual(item["action"], "draft:dispute" if verdict == "finding" else "none")
                        if period is None:
                            self.assertNotIn("period", item)
                        else:
                            self.assertEqual(item["period"], period)
                        if verdict == "finding":
                            self.assertEqual(item["amount_usd"], "10.00")

    def test_changed_amount_supersedes_and_repeat_preserves_history(self):
        first = report("2045-02-15", "supervised", str(self.state))["findings"][0]
        batch = self.root / "batch_02"
        batch.mkdir()
        message = EmailMessage()
        message["From"] = "Morgan Hale <morgan@example.test>"
        message["To"] = "AP <ap@customer.test>"
        message["Date"] = "Thu, 16 Feb 2045 12:00:00 +0000"
        message["Subject"] = "Partial credit"
        message.set_content("Credit issued USD 1.00 against BILL-7 freight.")
        (batch / "credit.eml").write_bytes(message.as_bytes())
        ingest(str(batch), str(self.state))
        second = report("2045-02-16", "supervised", str(self.state))["findings"][0]
        repeated = report("2045-02-16", "supervised", str(self.state))["findings"][0]
        self.assertEqual(second["supersedes"], [first["id"]])
        self.assertEqual(second, repeated)
        self.assertNotEqual(first["id"], second["id"])

    def test_missing_document_resolved_has_parent_revision(self):
        self.verdict = "cannot_determine"
        before = report("2045-02-15", "supervised", str(self.state))["findings"][0]
        self.verdict = "finding"
        after = report("2045-02-16", "supervised", str(self.state))["findings"][0]
        self.assertEqual(after["supersedes"], [before["id"]])

    def test_bad_quote_repaired_from_original_sources(self):
        self.bad_quote = True
        result = report("2045-02-15", "supervised", str(self.state))
        self.assertIn("repair", self.calls)
        self.assertEqual(result["findings"][0]["evidence"][0]["quote"], "base freight USD 110.00 for 50 kg.")

    def test_feedback_override_and_close_survive_command_boundaries(self):
        first = report("2045-02-15", "supervised", str(self.state))
        path = self.root / "feedback.json"
        envelope = {"after_batch": "batch_01", "decided_at": "2045-02-16T10:00:00Z", "items": [{
            "item_id": "manual-override", "finding_key": {"invoice_id": "BILL-7", "class": "overcharge"},
            "decision": "override_amount", "fact": {"value": {"amount_usd": "7.77"}},
            "scope": {"kind": "one_off", "applies_to": {"invoice_id": "BILL-7"}}}]}
        path.write_text(json.dumps(envelope))
        feedback(str(path), str(self.state))
        self.assertEqual(report("2045-02-15", "supervised", str(self.state))["findings"][0]["amount_usd"], "10.00")
        after = report("2045-02-16", "supervised", str(self.state))["findings"][0]
        self.assertEqual(after["amount_usd"], "7.77")
        self.assertEqual(after["learned_at"], {"feedback_item_id": "manual-override", "batch": "batch_01"})
        envelope["decided_at"] = "2045-02-17T10:00:00Z"
        envelope["items"][0].update(item_id="manual-close", decision="close")
        path.write_text(json.dumps(envelope))
        feedback(str(path), str(self.state))
        closed = report("2045-02-17", "supervised", str(self.state))["findings"][0]
        self.assertEqual(closed["verdict"], "no_finding")
        self.assertEqual(closed["action"], "none")
        self.assertEqual(closed["learned_at"], {"feedback_item_id": "manual-close", "batch": "batch_01"})


if __name__ == "__main__":
    unittest.main()
