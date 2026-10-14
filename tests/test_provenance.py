"""Adversarial retrieval/provenance regressions. Synthetic records, no inference."""
import copy
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "harness"))
from pipeline import Evidence, build_units, unique_candidates
from policy import actions, feedback_effects
from money import calculate


def record(name, contracts=(), invoices=(), billed=(), kinds=(), text="", global_=False):
    return {"email": "batch_01/" + name + ".eml", "received_at": "2045-02-01T00:00:00+00:00",
            "documents": [{"doc": "batch_01/" + name + ".eml#record.xlsx", "pages": [text], "scan_pages": []}],
            "index": {"contract_ids": list(contracts), "invoice_ids": list(invoices),
                      "billed_invoice_ids": list(billed), "shipment_ids": [], "kinds": list(kinds), "global": global_}}


class RetrievalTests(unittest.TestCase):
    def fixture(self, resend="BILL-7"):
        return [record("contract", ["AG-1"], kinds=["agreement"]),
                record("original", ["AG-1"], ["BILL-7"], ["BILL-7"], ["invoice"]),
                record("resend", invoices=[resend], billed=[resend], kinds=["invoice"])]

    def test_original_and_unassigned_resend_have_one_owner(self):
        units = build_units(self.fixture(), "2045-02-15", [])
        self.assertEqual([(u["focus_contract"], u["target_invoice_ids"]) for u in units], [("AG-1", ["BILL-7"])])

    def test_ownership_independent_of_resend_order_and_spacing(self):
        for records in (self.fixture("bill # : - 7"), list(reversed(self.fixture("bill -7")))):
            with self.subTest(records=records):
                units = build_units(records, "2045-02-15", [])
                self.assertEqual(sum((u["target_invoice_ids"] for u in units), []), ["BILL-7"])

    def test_materially_different_hyphens_stay_distinct(self):
        records = self.fixture("B-ILL7")
        units = build_units(records, "2045-02-15", [])
        self.assertEqual(sorted(sum((u["target_invoice_ids"] for u in units), [])), ["B-ILL7", "BILL-7"])

    def test_ledger_canonical_matches_keep_raw_rows_and_exclude_collisions(self):
        rows = ["bill -7 | paid", "BILL # : - 7 | scheduled", "BILL-70 | unrelated", "ABILL-7 | unrelated", "B-ILL7 | unrelated"]
        records = self.fixture()[:2] + [record("ledger", invoices=["BILL-7"], kinds=["ledger"],
                    text="h1\nh2\nh3\nh4\n" + "\n".join(rows), global_=True)]
        units = build_units(records, "2045-02-15", [])
        text = next(d["pages"][0]["text"] for d in units[0]["documents"] if "ledger" in d["doc"])
        for row in rows[:2]:
            self.assertIn(row, text)
        for row in rows[2:]:
            self.assertNotIn(row, text)

    def test_final_uniqueness_rejects_duplicate_keys_preserves_classes(self):
        item = {"class": "overcharge", "invoice_id": "BILL-7"}
        with self.assertRaises(ValueError):
            unique_candidates([item, dict(item, invoice_id="bill -7")])
        other = dict(item, **{"class": "duplicate"})
        self.assertEqual(unique_candidates([item, other]), [item, other])


def citation(doc, quote):
    return {"doc": doc, "page": 1, "quote": quote}


def monetary_fixture():
    bill = citation("batch_01/bill.eml#bill.pdf", "Freight billed USD 110.00; chargeable weight 50 kg.")
    rate = citation("batch_01/terms.eml#terms.pdf", "Freight rate USD 2.00 per kg.")
    evidence = Evidence([{"documents": [{"doc": ev["doc"], "pages": [ev["quote"]]} for ev in [bill, rate]]}])
    inputs = [{"name": name, "value": value, "source_text": value, "evidence": ev}
              for name, value, ev in [("billed", "110.00", bill), ("weight", "50", bill), ("rate", "2.00", rate)]]
    item = {"class": "overcharge", "invoice_id": "BILL-7", "verdict": "finding", "evidence": [bill, rate],
            "calculation": {"currency": "USD", "inputs": inputs,
                "steps": [{"name": "correct", "expression": "cents(rate * weight)", "evidence": [rate, bill]}],
                "amount": "max(0, billed - correct)"}}
    return evidence, item


class CalculationProvenanceTests(unittest.TestCase):
    def test_escaped_citation_whitespace_decodes_only_when_held_text_matches(self):
        for invented in (False, True):
            evidence, item = monetary_fixture()
            bill = item['calculation']['inputs'][0]['evidence']
            actual = bill['quote'].replace('; ', ';\n')
            evidence.docs[bill['doc']]['pages'] = [actual]
            bill['quote'] = actual.replace('\n', '\\\\n')
            if invented: bill['quote'] = bill['quote'].replace('110.00', '999.00')
            self.assertEqual(not evidence.verify([item]), not invented)
            if not invented: self.assertEqual(bill['quote'], actual)

    def test_scalar_citation_recovers_only_a_unique_exact_identified_suffix(self):
        actual = 'ORIGIN-DEST AWB XY65432199 Delivery fee 1 shipment 17.64 17.64 collected 2045-01-12'
        for supplied, copies, succeeds in [
                (actual.replace('ORIGIN-DEST', 'DEST-ORIGIN'), 1, True),
                (actual.replace('XY65432199', 'XY65432198'), 1, False),
                (actual.replace('Delivery fee', 'Storage fee'), 1, False),
                (actual.replace('17.64', '17.65'), 1, False),
                (actual.replace('ORIGIN-DEST', 'DEST-ORIGIN'), 2, False)]:
            with self.subTest(supplied=supplied, copies=copies):
                bill = citation('batch_01/synthetic.eml#invoice.pdf', actual)
                input_cite = dict(bill, quote=supplied)
                evidence = Evidence([{'documents': [{'doc': bill['doc'], 'pages': [actual] * copies}]}])
                item = {'class': 'overcharge', 'invoice_id': 'SYNTH-4', 'verdict': 'finding', 'evidence': [bill],
                        'calculation': {'inputs': [{'name': 'fee', 'value': '17.64', 'source_text': '17.64',
                                                   'evidence': input_cite}], 'steps': [], 'amount': 'fee'}}
                errors = evidence.verify([item])
                self.assertEqual(not errors, succeeds)
                if succeeds:
                    self.assertIn(input_cite['quote'], actual)
                    self.assertIn('XY65432199 Delivery fee', input_cite['quote'])

    def test_exact_unit_decorated_numeric_sources_preserve_value_and_boundaries(self):
        for token, value in [('42kg', '42'), ('7.25%', '7.25'), ('INR1,234.50', '1234.50'), ('42 kg', '42')]:
            with self.subTest(token=token):
                ev = citation('batch_01/scan.eml#invoice.pdf', 'Measured charge: ' + token + ' exactly.')
                evidence = Evidence([{'documents': [{'doc': ev['doc'], 'pages': [ev['quote']]}]}])
                item = {'class': 'overcharge', 'invoice_id': 'SYNTH-9', 'verdict': 'finding', 'evidence': [ev],
                        'calculation': {'currency': 'USD', 'inputs': [{'name': 'source', 'value': value,
                            'source_text': token, 'evidence': ev}], 'steps': [], 'amount': 'source'}}
                self.assertEqual(evidence.verify([item]), [])
                item['calculation']['inputs'][0]['value'] = '999'
                self.assertTrue(evidence.verify([item]))
        for token in ['AWB42', '42kg99', '42widgets', '7.25%invented']:
            with self.subTest(invalid=token):
                evidence, item = monetary_fixture()
                ev = citation('batch_01/scan.eml#invoice.pdf', 'Measured charge: ' + token + ' exactly.')
                evidence.docs[ev['doc']] = {'doc': ev['doc'], 'pages': [ev['quote']]}
                item['calculation']['inputs'][0].update(value='42', source_text=token, evidence=ev)
                self.assertTrue(evidence.verify([item]))
        for text in ['AWB42kg', '42kg99', '142kg']:
            evidence, item = monetary_fixture()
            ev = citation('batch_01/scan.eml#invoice.pdf', 'Measured charge: ' + text + ' exactly.')
            evidence.docs[ev['doc']] = {'doc': ev['doc'], 'pages': [ev['quote']]}
            item['calculation']['inputs'][0].update(value='42', source_text='42kg', evidence=ev)
            self.assertTrue(evidence.verify([item]))

    def test_source_bound_inputs_derive_unprinted_recovery(self):
        evidence, item = monetary_fixture()
        self.assertEqual(evidence.verify([item]), [])
        self.assertEqual(calculate(item["calculation"])[0], "10.00")

    def test_valid_quote_cannot_support_invented_amount_or_input(self):
        evidence, item = monetary_fixture()
        item["calculation"] = {"steps": [], "amount": "999999"}
        self.assertTrue(evidence.verify([item]))
        evidence, item = monetary_fixture()
        item["calculation"]["inputs"][0].update(value="999999", source_text="999999")
        self.assertTrue(evidence.verify([item]))

    def test_missing_input_provenance_and_numeric_substring_fail(self):
        evidence, item = monetary_fixture()
        item["calculation"]["inputs"][1].pop("evidence")
        self.assertTrue(evidence.verify([item]))
        evidence, item = monetary_fixture()
        item["calculation"]["inputs"][0].update(value="10.00", source_text="10.00")
        self.assertTrue(evidence.verify([item]))

    def test_model_amount_cannot_override_decimal_rounding(self):
        evidence, item = monetary_fixture()
        item["amount_usd"] = "9.99"
        self.assertTrue(evidence.verify([item]))

    def test_material_literal_cannot_hide_in_formula(self):
        evidence, item = monetary_fixture()
        item["calculation"]["amount"] = "billed - 3.25 * weight"
        self.assertTrue(evidence.verify([item]))

    def test_glossary_constants_cannot_be_used_as_invented_charges(self):
        for expression in ["billed + 30", "billed * 100", "billed + '100'", "billed / 30", "billed - 0"]:
            evidence, item = monetary_fixture()
            item["calculation"]["amount"] = expression
            with self.subTest(expression=expression):
                self.assertTrue(evidence.verify([item]))

    def test_percent_divisor_and_source_bound_half_up_recompute(self):
        evidence, item = monetary_fixture()
        token = {"doc": "batch_01/terms.eml#terms.pdf", "page": 1, "quote": "Fuel percent 10.00; rounded base 10.05."}
        evidence.docs[token["doc"]]["pages"].append(token["quote"])
        token["page"] = 2
        item["calculation"] = {"inputs": [{"name": name, "value": value, "source_text": value, "evidence": token}
            for name, value in [("percent", "10.00"), ("base", "10.05")]], "amount": "cents(percent / 100 * base)"}
        self.assertEqual(evidence.verify([item]), [])
        self.assertEqual(calculate(item["calculation"])[0], "1.01")
        item["amount_usd"] = "1.00"
        self.assertTrue(evidence.verify([item]))

    def test_dates_and_month_constant_bound_to_fee_formula(self):
        evidence, item = monetary_fixture()
        ev = {"doc": "batch_01/dates.eml", "page": 1, "quote": "Due 2045-01-01, paid 2045-02-01; monthly fee USD 3.00."}
        evidence.docs[ev["doc"]] = {"doc": ev["doc"], "pages": [ev["quote"]]}
        item["calculation"] = {"inputs": [{"name": "fee", "value": "3.00", "source_text": "3.00", "evidence": ev}],
            "dates": [{"value": value, "evidence": ev} for value in ["2045-01-01", "2045-02-01"]],
            "amount": 'fee * ceil(days("2045-02-01", "2045-01-01") / 30)'}
        self.assertEqual(evidence.verify([item]), [])
        self.assertEqual(calculate(item["calculation"])[0], "6.00")
        item["calculation"]["dates"].pop()
        self.assertTrue(evidence.verify([item]))

    def test_function_name_input_cannot_decorate_source_free_formula(self):
        evidence, item = monetary_fixture()
        item["calculation"]["inputs"][0]["name"] = "max"
        item["calculation"]["steps"] = []
        item["calculation"]["amount"] = "max(0, 0)"
        self.assertTrue(evidence.verify([item]))


def authority_fixture():
    doc = "batch_01/terms.eml#agreement.pdf"
    signed = citation(doc, "Transport agreement AG-1. Signed by both parties on 2045-01-01.")
    terms = citation(doc, "Freight rate is USD 2.00 per kg.")
    contact = citation(doc, "Schedule 1: billing_disputes contact Billing Desk <billing@example.test>.")
    pickup = citation("batch_01/tracking.eml", "PICKED_UP shipment SHIP-7 on 2045-02-01.")
    bill = citation("batch_01/bill.eml#bill.pdf", "Invoice BILL-7 freight USD 110.00 for 50 kg.")
    records = [{"documents": [{"doc": doc, "pages": ["\n".join(ev["quote"] for ev in [signed, terms, contact])]},
                              {"doc": pickup["doc"], "pages": ["From: Carrier <billing@example.test>\n\n" + pickup["quote"]]},
                              {"doc": bill["doc"], "pages": [bill["quote"]]}]}]
    item = {"class": "overcharge", "invoice_id": "BILL-7", "verdict": "finding", "amount_usd": "10.00",
        "evidence": [terms, bill], "confidence_signals": {"complete": True, "effective": True, "authoritative": True},
        "context": {"contract_id": "AG-1", "terms": [{"authority": "signed", "evidence": terms, "signature_evidence": signed}],
                    "facts": [bill, pickup], "recipient": {"address": "billing@example.test", "category": "billing_disputes",
                                "evidence": contact, "signature_evidence": signed},
                    "pickup": {"kind": "carrier_email", "date": "2045-02-01", "evidence": pickup,
                               "carrier_evidence": contact, "signature_evidence": signed}}}
    return records, item


class AuthorityProvenanceTests(unittest.TestCase):
    def run_action(self, records, item, mode="autonomous"):
        evidence = Evidence(records)
        actions([item], mode, "2045-02-15", evidence.valid, evidence.authority_supported)
        return item["action"]

    def test_explicit_signed_schedule_and_pickup_support_execute(self):
        self.assertEqual(self.run_action(*authority_fixture()), "execute:dispute")

    def document_request_fixture(self):
        records, item = authority_fixture()
        reference = citation("batch_01/reference.eml", "Pricing requires Annex A revision 2, held by Billing Desk.")
        records[0]["documents"].append({"doc": reference["doc"], "pages": ["From: Carrier <billing@example.test>\n\n" + reference["quote"]]})
        item.update(verdict="cannot_determine", missing_doc={"name": "Annex A rev 2", "holder": {"person": "Billing Desk"}}, evidence=[reference])
        item["confidence_signals"].update(complete=False, authoritative=False)
        item["context"]["terms"][0]["signature_evidence"] = None
        item["context"]["pickup"] = {}
        return records, item

    def test_document_request_uses_diagnosis_and_signed_holder_not_unknown_prices(self):
        self.assertEqual(self.run_action(*self.document_request_fixture()), "execute:request_document")

    def test_document_request_holds_without_bound_name_holder_or_signed_contact(self):
        for invalid in ("name", "holder", "signature"):
            records, item = self.document_request_fixture()
            if invalid == "name": item["missing_doc"]["name"] = "Annex A rev 20"
            elif invalid == "holder": item["missing_doc"]["holder"]["person"] = "Unknown Desk"
            else: item["context"]["recipient"]["signature_evidence"] = None
            self.assertEqual(self.run_action(records, item), "human_review")

    def test_document_request_preserves_conflict_and_aggregate_threshold_gates(self):
        for verdict, amount in [("conflict", "1.00"), ("finding", "5000.00")]:
            records, item = self.document_request_fixture()
            _, other = authority_fixture()
            other.update(verdict=verdict, amount_usd=amount)
            evidence = Evidence(records)
            actions([item, other], "autonomous", "2045-02-15", evidence.valid, evidence.authority_supported)
            self.assertEqual(item["action"], "human_review")

    def test_execution_quote_uses_signature_blocks_on_held_cited_page(self):
        records, item = authority_fixture()
        signature = item["context"]["terms"][0]["signature_evidence"]
        page = records[0]["documents"][0]["pages"][0]
        page = page.replace(signature["quote"], "Executed by duly authorised officers.\n"
             "Signed for and on behalf of Sender Ltd\nName: Anne\nDate: 2045-01-01\n"
             "Signed for and on behalf of Carrier Ltd\nName: Bill\nDate: 2045-01-01")
        signature["quote"] = "Executed by duly authorised officers."
        records[0]["documents"][0]["pages"][0] = page
        self.assertEqual(self.run_action(records, item), "execute:dispute")
        records[0]["documents"][0]["pages"][0] = page.replace("Signed for and on behalf of", "Unsigned for")
        self.assertEqual(self.run_action(records, item), "human_review")

    def test_signature_blocks_on_other_page_cannot_complete_execution_quote(self):
        records, item = authority_fixture()
        signature = item["context"]["terms"][0]["signature_evidence"]
        doc = records[0]["documents"][0]
        doc["pages"][0] = doc["pages"][0].replace(signature["quote"], "Executed by duly authorised officers.")
        signature["quote"] = "Executed by duly authorised officers."
        doc["pages"].append("Signed for and on behalf of Sender\nSigned for and on behalf of Carrier")
        self.assertEqual(self.run_action(records, item), "human_review")

    def test_original_authorized_waiver_can_use_its_signed_schedule_for_recipient(self):
        records, item = authority_fixture()
        context = item["context"]
        term = context["terms"][0]
        schedule = citation(term["evidence"]["doc"], "Schedule 1: waivers contact Finance <finance@example.test>.")
        records[0]["documents"][0]["pages"][0] += "\n" + schedule["quote"]
        waiver = citation("batch_01/waiver.eml", "We waive the delivery fee on shipment SHIP-7.")
        records[0]["documents"].append({"doc": waiver["doc"], "pages": ["From: Finance <finance@example.test>\n\n" + waiver["quote"]]})
        context["terms"] = [{"authority": "schedule1", "category": "waivers", "evidence": waiver,
                             "schedule_evidence": schedule, "signature_evidence": term["signature_evidence"]}]
        item["class"] = "waived_but_billed"
        self.assertEqual(self.run_action(records, item), "execute:dispute")
        context["terms"][0].pop("signature_evidence")
        self.assertEqual(self.run_action(records, item), "human_review")

    def test_original_carrier_reply_keeps_top_message_authority_only(self):
        records, item = authority_fixture()
        document = records[0]["documents"][1]
        document["pages"][0] += "\nOn 2045-02-01, Customer wrote:\n> Please confirm pickup of SHIP-7."
        self.assertEqual(self.run_action(records, item), "execute:dispute")
        item["context"]["pickup"]["evidence"]["quote"] = "Please confirm pickup of SHIP-7."
        self.assertEqual(self.run_action(records, item), "human_review")

    def test_dated_execution_statement_is_affirmative_proof(self):
        records, item = authority_fixture()
        signature = item["context"]["terms"][0]["signature_evidence"]
        old = signature["quote"]
        signature["quote"] = "Signed on 2045-01-01 by both parties."
        records[0]["documents"][0]["pages"][0] = records[0]["documents"][0]["pages"][0].replace(old, signature["quote"])
        self.assertEqual(self.run_action(records, item), "execute:dispute")

    def test_operational_pickup_sender_needs_issuer_identity_not_term_authority(self):
        records, item = authority_fixture()
        records[0]["documents"][1]["pages"][0] = records[0]["documents"][1]["pages"][0].replace(
            "billing@example.test", "operations@example.test")
        item["context"]["pickup"]["carrier_evidence"] = None
        item["context"]["pickup"]["signature_evidence"] = None
        self.assertEqual(self.run_action(records, item), "execute:dispute")
        records[0]["documents"][1]["pages"][0] = records[0]["documents"][1]["pages"][0].replace(
            "operations@example.test", "operations@outside.test")
        self.assertEqual(self.run_action(records, item), "human_review")

    def test_pickup_event_in_original_carrier_email_is_not_a_fake_spreadsheet(self):
        records, item = authority_fixture()
        item["context"]["pickup"]["kind"] = "tracking"
        self.assertEqual(self.run_action(records, item), "execute:dispute")

    def test_contact_address_sentence_punctuation_is_not_part_of_domain(self):
        records, item = authority_fixture()
        contact = item["context"]["recipient"]["evidence"]
        old = contact["quote"]
        contact["quote"] = old.replace("<", "").replace(">", "")
        records[0]["documents"][0]["pages"][0] = records[0]["documents"][0]["pages"][0].replace(old, contact["quote"])
        self.assertEqual(self.run_action(records, item), "execute:dispute")

    def test_unsupported_signed_label_and_contradictory_unsigned_hold(self):
        records, item = authority_fixture()
        item["context"]["terms"][0].pop("signature_evidence")
        self.assertEqual(self.run_action(records, item), "human_review")
        records, item = authority_fixture()
        records[0]["documents"][0]["pages"][0] += "\nUNSIGNED DRAFT - not executed."
        self.assertEqual(self.run_action(records, item), "human_review")

    def test_forwarded_or_quoted_authority_does_not_execute(self):
        for prefix in ["Forwarded message:\n", "> "]:
            records, item = authority_fixture()
            records[0]["documents"][0]["pages"][0] = prefix + records[0]["documents"][0]["pages"][0]
            self.assertEqual(self.run_action(records, item), "human_review")

    def test_wrong_contact_category_or_fake_pickup_holds(self):
        records, item = authority_fixture()
        item["context"]["recipient"]["category"] = "sales"
        self.assertEqual(self.run_action(records, item), "human_review")
        records, item = authority_fixture()
        item["context"]["pickup"]["evidence"] = item["context"]["terms"][0]["evidence"]
        self.assertEqual(self.run_action(records, item), "human_review")

    def test_supervised_drafts_despite_incomplete_provenance(self):
        records, item = authority_fixture()
        item["context"]["terms"][0].pop("signature_evidence")
        self.assertEqual(self.run_action(records, item, "supervised"), "draft:dispute")

    def test_conditional_signature_clause_is_not_execution_proof(self):
        records, item = authority_fixture()
        signature = item["context"]["terms"][0]["signature_evidence"]
        replacement = "An extension is effective only if signed by both parties."
        records[0]["documents"][0]["pages"][0] = records[0]["documents"][0]["pages"][0].replace(signature["quote"], replacement)
        signature["quote"] = replacement
        self.assertEqual(self.run_action(records, item), "human_review")

    def test_executed_signature_blocks_and_separate_schedule_header_pass(self):
        records, item = authority_fixture()
        signature = item["context"]["terms"][0]["signature_evidence"]
        replacement = ("Executed by duly authorised officers.\nSigned for and on behalf of Shipper Labs:\n"
                       "Signature: Jordan Finch\nSigned for and on behalf of Carrier Freight:\nSignature: Avery Reed")
        page = records[0]["documents"][0]["pages"][0].replace(signature["quote"], replacement)
        page = page.replace("Schedule 1: billing", "Schedule 1\nbilling")
        records[0]["documents"][0]["pages"][0] = page
        signature["quote"] = replacement
        item["context"]["recipient"]["evidence"]["quote"] = "billing_disputes contact Billing Desk <billing@example.test>."
        self.assertEqual(self.run_action(records, item), "execute:dispute")

    def test_whole_schedule_quote_cannot_borrow_another_contacts_category(self):
        records, item = authority_fixture()
        page = records[0]["documents"][0]["pages"][0]
        page += "\nSales contact Outsider <sales@example.test>."
        records[0]["documents"][0]["pages"][0] = page
        item["context"]["recipient"]["address"] = "sales@example.test"
        item["context"]["recipient"]["evidence"]["quote"] = page
        self.assertEqual(self.run_action(records, item), "human_review")

    def test_negated_pickup_cannot_be_execution_proof(self):
        records, item = authority_fixture()
        old = item["context"]["pickup"]["evidence"]["quote"]
        new = "Shipment SHIP-7 was not picked up on 2045-02-01."
        records[0]["documents"][1]["pages"][0] = records[0]["documents"][1]["pages"][0].replace(old, new)
        item["context"]["pickup"]["evidence"]["quote"] = new
        self.assertEqual(self.run_action(records, item), "human_review")

    def test_model_cannot_forge_human_feedback_authority(self):
        records, item = authority_fixture()
        item["learned_at"] = {"feedback_item_id": "invented", "batch": "batch_01"}
        item["context"]["terms"][0].update(authority="feedback", feedback_item_id="invented")
        feedback_effects([item], [])
        self.assertNotIn("learned_at", item)
        self.assertEqual(self.run_action(records, item), "human_review")

    def test_rates_contact_cannot_waive_by_claiming_rates_category(self):
        for category, expected in [("rates", "human_review"), ("waivers", "execute:dispute")]:
            records, item = authority_fixture()
            item["class"] = "waived_but_billed"
            doc = records[0]["documents"][0]["doc"]
            entry = citation(doc, f"{category} contact Terms Desk <terms@example.test>.")
            records[0]["documents"][0]["pages"][0] += "\n" + entry["quote"]
            waiver = citation("batch_01/waiver.eml", "I waive handling for shipment SHIP-7.")
            records[0]["documents"].append({"doc": waiver["doc"], "pages": [
                "From: Terms Desk <terms@example.test>\n\n" + waiver["quote"]]})
            item["context"]["terms"].append({"authority": "schedule1", "category": category,
                "evidence": waiver, "schedule_evidence": entry,
                "signature_evidence": item["context"]["terms"][0]["signature_evidence"]})
            with self.subTest(category=category):
                self.assertEqual(self.run_action(records, item), expected)


if __name__ == "__main__":
    unittest.main()
