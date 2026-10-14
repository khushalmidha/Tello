"""Semantic indexing, source retrieval, two-stage auditing and deterministic report assembly."""
from __future__ import annotations

import copy
import calendar
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

from grader.extract import norm
from grader.validate import schema_errors
from model import Model, PROMPT_VERSION
from money import calculate, calculation_values, cents, decimal, evaluate, usd
from policy import actions, confidence, feedback_effects, invoice_id, invoice_in_text, key, open_feedback
from prompts import AUDIT, INDEX, OUTPUT, VERIFY
from provenance import AuthorityProof, calculation_provenance
from reader import document_payload, read_email
from store import Store, digest

ROOT = Path(__file__).resolve().parent.parent
WORKERS = 1 if os.environ.get("ANTHROPIC_BASE_URL", "").startswith(("http://127.0.0.1:", "http://localhost:")) else 12
MAX_UNIT_INVOICES = 6


def cutoff(as_of: str) -> str:
    date.fromisoformat(as_of)
    return as_of + "T23:59:59.999999+00:00"


def ingest(batch_dir: str, state_dir: str) -> None:
    store = Store(state_dir)
    folder = Path(batch_dir)
    if not folder.is_dir():
        raise ValueError(f"not a batch directory: {folder}")
    manifest = store.read("manifest.json", {"version": 1, "records": {}})
    for path in sorted(folder.glob("*.eml")):
        content_hash = digest(path.read_bytes())
        email = f"{folder.name}/{path.name}"
        old = manifest["records"].get(email)
        if old and old["hash"] == content_hash:
            continue
        record = read_email(path, folder.name)
        name = f"records/{digest(email)}.json"
        store.write(name, record)
        manifest["records"][email] = {"hash": content_hash, "file": name}
    # Manifest is committed last. Re-ingesting identical content has no effect.
    store.write("manifest.json", manifest)


def feedback(feedback_file: str, state_dir: str) -> None:
    store = Store(state_dir)
    record = json.loads(Path(feedback_file).read_text(encoding="utf-8-sig"))
    for field in ("after_batch", "decided_at", "items"):
        if field not in record:
            raise ValueError(f"feedback missing {field}")
    datetime.fromisoformat(record["decided_at"].replace("Z", "+00:00"))
    decisions = {"confirm", "reject", "close", "override_amount", "resolve_conflict", "one_off_approval"}
    existing = {item["item_id"]: item for previous in store.records("feedback") for item in previous["items"]}
    for item in record["items"]:
        key(item["finding_key"])
        if item.get("decision") not in decisions or item.get("scope", {}).get("kind") not in ("standing", "one_off"):
            raise ValueError("unsupported feedback decision or scope")
        if item["item_id"] in existing and existing[item["item_id"]] != item:
            raise ValueError(f"feedback id reused with different facts: {item['item_id']}")
        existing[item["item_id"]] = item
    store.write(f"feedback/{digest(record)}.json", record)


def available(store: Store, as_of: str) -> list[dict]:
    end = datetime.fromisoformat(cutoff(as_of))
    manifest = store.read("manifest.json", {"records": {}})
    records = [store.read(entry["file"]) for entry in manifest["records"].values()]
    return sorted((record for record in records if datetime.fromisoformat(record["received_at"]) <= end),
                  key=lambda r: (r["received_at"], r["email"]))


def index_records(records: list[dict], model: Model, store: Store) -> list[dict]:
    indexed, pending = [], []
    for record in records:
        index_hash = digest([model.cache_identity, INDEX, record])
        cached = store.read(f"index/{index_hash}.json")
        if cached is not None:
            indexed.append(dict(record, index=cached))
        else:
            pending.append((record, index_hash))
    chunks, current, size = [], [], 0
    for pair in pending:
        length = len(json.dumps(pair[0]))
        if current and size + length > 60000:
            chunks.append(current)
            current, size = [], 0
        current.append(pair)
        size += length
    if current:
        chunks.append(current)

    def run(chunk):
        result = model.ask("document_index", INDEX, {"emails": [dict(email=r["email"],
                           documents=document_payload([r])) for r, _ in chunk]}, max_tokens=6500)
        mapping = {entry["email"]: entry for entry in result.get("records", [])}
        expected = {record["email"] for record, _ in chunk}
        if set(mapping) != expected or len(result.get("records", [])) != len(expected):
            raise ValueError("document index must cover every email exactly once")
        output = []
        for record, index_hash in chunk:
            entry = mapping[record["email"]]
            for field in ("contract_ids", "invoice_ids", "billed_invoice_ids", "shipment_ids", "kinds"):
                if not isinstance(entry.get(field), list):
                    raise ValueError(f"document index lacks {field}")
            for field in ("invoice_ids", "billed_invoice_ids"):
                entry[field] = sorted({invoice_id(value) for value in entry[field]})
            store.write(f"index/{index_hash}.json", entry)
            output.append(dict(record, index=entry))
        return output

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for output in pool.map(run, chunks):
            indexed.extend(output)
    return sorted(indexed, key=lambda record: record["email"])


def build_units(records: list[dict], as_of: str, feedback_items: list[dict]) -> list[dict]:
    """Use exact extracted identifiers for joins; no carrier-specific formats or layout rules."""
    records = copy.deepcopy(records)
    for record in records:
        for field in ("invoice_ids", "billed_invoice_ids"):
            record["index"][field] = sorted({invoice_id(value) for value in record["index"][field]})
    contracts = {cid for r in records if "agreement" in r["index"]["kinds"] for cid in r["index"]["contract_ids"]}
    assignments = {}
    groups = {contract: set() for contract in contracts}
    for record in records:
        ix = record["index"]
        if not ix["billed_invoice_ids"]:
            continue
        known = set(ix["contract_ids"]) & contracts
        for identifier in ix["billed_invoice_ids"]:
            assignments.setdefault(identifier, set()).update(known)
    # Ownership is decided once per printed identity, after every representation is seen.
    # Conflicting agreement references remain one unknown unit with all instruments held.
    for identifier, known in assignments.items():
        group = next(iter(known)) if len(known) == 1 else "__unassigned__"
        groups.setdefault(group, set()).add(identifier)
    units = []
    for contract, ids in sorted(groups.items()):
        is_unknown = contract == "__unassigned__"
        sources = []
        primary = [r for r in records if (contract in r["index"]["contract_ids"] if not is_unknown else
                   bool(set(r["index"]["billed_invoice_ids"]) & ids))]
        related_invoices = set(ids)
        related_shipments = {awb for r in primary for awb in r["index"]["shipment_ids"]}
        # Closure finds invoices billed months apart, payment references and separate supporting emails.
        for _ in range(3):
            related = [r for r in records if set(r["index"]["invoice_ids"]) & related_invoices or
                       set(r["index"]["shipment_ids"]) & related_shipments]
            for r in related:
                if not r["index"].get("global"):
                    related_invoices.update(r["index"]["invoice_ids"])
                    related_shipments.update(r["index"]["shipment_ids"])
        for record in records:
            ix = record["index"]
            include = record in primary or ix.get("global") or bool(set(ix["invoice_ids"]) & related_invoices)
            include = include or bool(set(ix["shipment_ids"]) & related_shipments)
            include = include or (is_unknown and bool(set(ix["kinds"]) & {"agreement", "amendment", "rate_notice"}))
            # A newly signed MSA can replace terms on bills still referring to its predecessor.
            # Let the auditor compare party/service/effective scope rather than hide a replacement
            # merely because its agreement identifier differs from the invoice's printed reference.
            include = include or bool(set(ix["kinds"]) & {"agreement", "amendment"})
            # Keep unlinked emails: unknown relationships must not silently erase a waiver or extension.
            include = include or (not ix["contract_ids"] and not ix["invoice_ids"] and not ix["shipment_ids"])
            if include:
                payload = document_payload([record])
                if set(ix["kinds"]) & {"tracking", "ledger"}:
                    identifiers = related_invoices | related_shipments
                    for document in payload:
                        if not document["doc"].lower().endswith(".xlsx"):
                            continue
                        for page in document["pages"]:
                            lines = page["text"].splitlines()
                            page["text"] = "\n".join(line for n, line in enumerate(lines) if n < 4 or
                                              any(invoice_in_text(identifier, line) for identifier in identifiers))
                sources.extend(payload)
        unit_feedback = [fb for fb in feedback_items if is_unknown or
                         fb.get("scope", {}).get("applies_to", {}).get("contract_id") in (None, contract)]
        common = {"as_of": as_of, "focus_contract": None if is_unknown else contract,
                  "documents": sources, "feedback": unit_feedback}
        targets = sorted(ids)
        for start in range(0, len(targets), MAX_UNIT_INVOICES):
            units.append(dict(common, target_invoice_ids=targets[start:start + MAX_UNIT_INVOICES],
                              include_portfolio=(start == 0 and not is_unknown)))
        if not targets and not is_unknown:
            units.append(dict(common, target_invoice_ids=[], include_portfolio=True))
    return units


class Evidence:
    def __init__(self, records):
        self.docs = {d["doc"]: d for r in records for d in r["documents"]}

    def valid(self, evidence) -> bool:
        if not isinstance(evidence, dict):
            return False
        document = self.docs.get(evidence.get("doc"))
        page = evidence.get("page")
        quote = evidence.get("quote")
        if document is None or not isinstance(page, int) or not isinstance(quote, str):
            return False
        if not 1 <= page <= len(document["pages"]) or len("".join(quote.split())) < 12:
            return False
        return norm(quote) in norm(document["pages"][page - 1])

    def authority_supported(self, item, remedy):
        return AuthorityProof(self).supports(item, remedy)

    def locate(self, value) -> None:
        """Recover verbatim citations in the same held document without inventing text."""
        if isinstance(value, list):
            for entry in value:
                self.locate(entry)
        elif isinstance(value, dict):
            if {"name", "value", "source_text", "evidence"} <= value.keys():
                citation = value["evidence"]
                if isinstance(citation, dict) and not self.valid(citation):
                    document = self.docs.get(citation.get("doc"))
                    quote, token = citation.get("quote"), value.get("source_text")
                    if document and isinstance(quote, str) and isinstance(token, str) and token:
                        # A scalar can use a verbatim suffix despite a mistaken
                        # leading label. Keep the shipment/invoice identifier,
                        # charge and numeric token; require one exact held match.
                        for word in re.finditer(r"\S+", quote):
                            suffix = quote[word.start():].strip()
                            if (len("".join(suffix.split())) < 40 or token not in suffix or
                                    not re.search(r"\b[A-Za-z]{2,}\d{4,}\b", suffix)):
                                continue
                            matches = [(i + 1, page) for i, page in enumerate(document["pages"])
                                       for _ in range(page.count(suffix))]
                            if len(matches) == 1:
                                citation.update(page=matches[0][0], quote=suffix)
                                break
            if {"doc", "page", "quote"} <= value.keys() and not self.valid(value):
                document = self.docs.get(value["doc"])
                quote = value["quote"]
                if document and isinstance(quote, str) and len("".join(quote.split())) >= 12:
                    decoded = re.sub(r"\\+([nrt])", lambda match: {"n": "\n", "r": "\r", "t": "\t"}[match[1]], quote)
                    decoded_matches = [i + 1 for i, page in enumerate(document["pages"])
                                       if norm(decoded) in norm(page)]
                    if decoded != quote and (self.valid(dict(value, quote=decoded)) or len(decoded_matches) == 1):
                        value["quote"] = quote = decoded
                    matches = [i + 1 for i, page in enumerate(document["pages"]) if norm(quote) in norm(page)]
                    if len(matches) == 1:
                        value["page"] = matches[0]
            for child in value.values():
                if isinstance(child, (dict, list)):
                    self.locate(child)

    def verify(self, candidates: list[dict]) -> list[str]:
        errors = []
        seen = set()
        for item in candidates:
            self.locate(item)
            try:
                finding_key = key(item)
                if item["class"] not in {"overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed",
                                          "late_fee_risk", "cashback", "expiry"}:
                    errors.append(f"unsupported finding class {item['class']}")
                if item["verdict"] not in {"finding", "no_finding", "conflict", "cannot_determine"}:
                    errors.append(f"unsupported verdict {item['verdict']}")
                if finding_key in seen:
                    errors.append(f"duplicate finding key {finding_key}")
                seen.add(finding_key)
                citations = list(item.get("evidence", []))
                if item["verdict"] != "no_finding" and not citations:
                    errors.append(f"{finding_key} lacks evidence")
                if item["verdict"] == "conflict" and len(citations) < 2:
                    errors.append(f"{finding_key} conflict needs both sources")
                plans = ([item["calculation"]] if item.get("calculation") and item["verdict"] == "finding" else [])
                plans += item.get("range_calculations", [])
                for plan in plans:
                    calculation_provenance(plan, self)
                    for step in plan.get("steps", []):
                        if not step.get("evidence"):
                            errors.append(f"{finding_key} step {step.get('name')} lacks source evidence")
                        citations.extend(step.get("evidence", []))
                citations.extend(item.get("risk_principal", {}).get("evidence", []))
                for citation in citations:
                    if not self.valid(citation):
                        errors.append(f"{finding_key} citation is not an exact held substring: {citation}")
                if item["verdict"] == "cannot_determine":
                    missing = item.get("missing_doc", {})
                    if not missing.get("name") or not (missing.get("holder", {}).get("person") or
                                                       missing.get("holder", {}).get("party")):
                        errors.append(f"{finding_key} missing document needs name and holder")
                if item["verdict"] == "finding" and item["class"] != "expiry":
                    computed = calculate(item["calculation"])[0]
                    if item.get("amount_usd") is not None and decimal(item["amount_usd"]) != decimal(computed):
                        errors.append(f"{finding_key} supplied amount differs from deterministic calculation")
                if item["verdict"] == "finding" and item["class"] in {"expiry", "late_fee_risk"}:
                    date.fromisoformat(item["deadline"])
                if item["verdict"] == "finding" and item["class"] == "late_fee_risk":
                    principal = item["risk_principal"]
                    calculation_provenance(principal, self, ("open", "rate_percent"))
                    values = calculation_values(principal)
                    if principal["currency"].upper() == "INR" and evaluate(principal["fx"], values) <= 0:
                        errors.append("late-fee risk FX must be positive")
                if item["verdict"] == "conflict":
                    if len(item.get("range_calculations", [])) != 2:
                        errors.append(f"{finding_key} conflict needs two calculation plans")
                    else:
                        for plan in item["range_calculations"]:
                            calculate(plan)
            except (ValueError, KeyError, TypeError, ArithmeticError, SyntaxError) as exc:
                errors.append(f"invalid candidate: {exc}")
        return errors


def signature(item):
    return {field: item.get(field) for field in ("class", "invoice_id", "contract_id", "period", "credit_type",
                                               "verdict", "amount_usd", "amount_range", "deadline")}


def unique_candidates(candidates: list[dict]) -> list[dict]:
    """Duplicate keys are an error, never an implicit last-writer-wins merge."""
    seen = set()
    for item in candidates:
        identity = key(item)
        if identity in seen:
            raise ValueError(f"duplicate finding key across work units: {identity}")
        seen.add(identity)
    return candidates


def run_unit(unit: dict, model: Model, evidence: Evidence, glossary: str) -> list[dict]:
    system = AUDIT + "\nAuthoritative task glossary:\n" + glossary + OUTPUT
    proposed = model.ask("audit", system, unit)
    reviewed = model.ask("review", VERIFY + "\nAuthoritative task glossary:\n" + glossary + OUTPUT,
                         dict(unit, proposed_audit=proposed))
    for attempt in range(2):
        candidates = reviewed.get("items", [])
        errors = evidence.verify(candidates)
        expected = set(unit["target_invoice_ids"])
        covered = {invoice_id(row["invoice_id"]) for row in reviewed.get("coverage", [])}
        if not expected <= covered:
            errors.append(f"missing invoice coverage: {sorted(expected - covered)}")
        for item in candidates:
            if item["class"] in ("cashback", "expiry"):
                if not unit["include_portfolio"] or item["contract_id"] != unit["focus_contract"]:
                    errors.append(f"portfolio key outside work unit: {key(item)}")
            elif invoice_id(item["invoice_id"]) not in expected:
                errors.append(f"invoice key outside work unit: {key(item)}")
        if not errors:
            break
        if attempt:
            raise ValueError("audit validation failed: " + "; ".join(errors[:5]))
        reviewed = model.ask("repair", VERIFY + "\nAuthoritative task glossary:\n" + glossary + OUTPUT,
                             dict(unit, proposed_audit=reviewed, validation_errors=errors))
    proposed_map = {}
    for item in proposed.get("items", []):
        try:
            value = copy.deepcopy(item)
            if value["verdict"] == "finding" and value["class"] != "expiry":
                value["amount_usd"] = calculate(value["calculation"])[0]
            if value["verdict"] == "conflict":
                value["amount_range"] = [calculate(plan)[0] for plan in value["range_calculations"]]
            proposed_map[key(value)] = signature(value)
        except (KeyError, ValueError, ArithmeticError):
            continue
    for item in candidates:
        if item.get("invoice_id"):
            item["invoice_id"] = invoice_id(item["invoice_id"])
        if item["verdict"] == "finding" and item["class"] != "expiry":
            item["amount_usd"], item["calculated_steps"] = calculate(item["calculation"])
        elif item["verdict"] == "conflict":
            item["amount_range"] = [calculate(plan)[0] for plan in item["range_calculations"]]
            if decimal(item["amount_range"][0]) > decimal(item["amount_range"][1]):
                raise ValueError("conflict range is reversed")
        item["review_agreed"] = proposed_map.get(key(item)) == signature(item)
    return candidates


def risk_amounts(candidates: list[dict]) -> None:
    disputed = {}
    for item in candidates:
        if item["verdict"] == "finding" and item["class"] in {"overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed"}:
            disputed[item["invoice_id"]] = disputed.get(item["invoice_id"], Decimal(0)) + decimal(item["amount_usd"])
    for item in candidates:
        if item["class"] != "late_fee_risk" or item["verdict"] != "finding":
            continue
        plan = item["risk_principal"]
        values = calculation_values(plan)
        currency = plan["currency"].upper()
        if currency not in {"USD", "INR"}:
            raise ValueError(f"unsupported late-fee currency: {currency}")
        fx = evaluate(plan["fx"], values) if currency == "INR" else Decimal(1)
        if fx <= 0:
            raise ValueError("late-fee risk FX must be positive")
        # Reconstruct original-currency disputes from calculation plans, avoiding USD rounding on each line.
        reductions = Decimal(0)
        for finding in candidates:
            if finding.get("invoice_id") != item["invoice_id"] or finding["verdict"] != "finding" or finding["class"] not in {
                "overcharge", "duplicate", "waived_but_billed", "late_fee_not_owed"}:
                continue
            if finding.get("learned_at") and "amount_usd" in finding and finding["amount_usd"] != calculate(finding["calculation"])[0]:
                reductions += decimal(finding["amount_usd"]) * fx
            else:
                original = dict(finding["calculation"], currency="USD")
                reductions += decimal(calculate(original)[0])
        principal = max(Decimal(0), evaluate(plan["open"], values) - reductions)
        item["amount_usd"] = usd(cents(principal * evaluate(plan["rate_percent"], values) / 100) / fx)


def report(as_of: str, mode: str, state_dir: str) -> dict:
    store = Store(state_dir)
    records = available(store, as_of)
    model = Model(store)
    evidence = Evidence(records)
    feedback_items = open_feedback(store.records("feedback"), as_of)
    if records:
        indexed = index_records(records, model, store)
        units = build_units(indexed, as_of, feedback_items)
        glossary = (ROOT / "GLOSSARY.md").read_text(encoding="utf-8")
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            candidates = [item for result in pool.map(lambda unit: run_unit(unit, model, evidence, glossary), units)
                          for item in result]
    else:
        candidates = []
    candidates = feedback_effects(unique_candidates(candidates), feedback_items)
    risk_amounts(candidates)
    filtered = []
    for item in candidates:
        if item["verdict"] == "finding" and item["class"] == "cashback":
            period = item["period"]
            if "-Q" in period:
                year, quarter = period.split("-Q")
                month = int(quarter) * 3
                period_end = date(int(year), month, calendar.monthrange(int(year), month)[1]).isoformat()
            else:
                period_end = item.get("context", {}).get("period_end")
                if not period_end:
                    raise ValueError("annual credit must identify its contract-year end date")
            if as_of <= period_end:
                continue
        if item["verdict"] == "finding" and item["class"] in ("expiry", "late_fee_risk"):
            days = (date.fromisoformat(item["deadline"]) - date.fromisoformat(as_of)).days
            if not ((0 <= days <= 60) if item["class"] == "expiry" else (0 < days <= 14)):
                continue
        if item["verdict"] == "finding" and item["class"] != "expiry" and decimal(item["amount_usd"]) <= 0:
            continue
        filtered.append(item)
    candidates = sorted(filtered, key=key)
    actions(candidates, mode, as_of, evidence.valid, evidence.authority_supported)
    history = [snapshot for snapshot in store.records("reports") if snapshot["as_of"] <= as_of]
    previous = max(history, key=lambda snapshot: (snapshot["as_of"], snapshot.get("revision", 0)), default={})
    previous_map = {key(item): item for item in previous.get("items", [])}
    current_keys = {key(item) for item in candidates}
    # A cleared item is an optional no_finding record, never an open dispute. Keeping
    # its decision provenance lets an auditor trace a changed answer after feedback.
    for decision in feedback_items:
        finding_key = key(decision["finding_key"])
        old = previous_map.get(finding_key)
        if decision["decision"] not in ("close", "reject") or old is None or finding_key in current_keys:
            continue
        cleared = {field: value for field, value in old.items() if field not in
                   {"id", "supersedes", "confidence", "amount_usd", "amount_range", "action", "retro"}}
        cleared.update(verdict="no_finding", action="none", amount_usd=None, amount_range=None,
                       learned_at={"feedback_item_id": decision["item_id"], "batch": decision["batch"]},
                       evidence=[ev for ev in old.get("evidence", []) if evidence.valid(ev)],
                       confidence_signals={"complete": True, "authoritative": True, "extraction": 1.,
                                           "agreement": 1., "effective": True, "ambiguity": 0.}, review_agreed=True)
        candidates.append(cleared)
        current_keys.add(finding_key)
    candidates.sort(key=key)
    output = {"schema_version": "1.0", "as_of": as_of, "mode": mode, "findings": [],
              "portfolio": {"cashback": [], "expiry": [], "late_fee_risk": []}}
    observed_models = sorted({entry["model"] for entry in store.records("model_cache") if entry.get("model")})
    output["model_provenance"] = {"observed_models": observed_models, "prompt_version": PROMPT_VERSION}
    fields = {"class", "invoice_id", "contract_id", "period", "credit_type", "verdict", "amount_usd",
              "amount_range", "deadline", "evidence", "missing_doc", "retro", "learned_at", "action"}
    for candidate in candidates:
        item = {field: value for field, value in candidate.items() if field in fields}
        if item.get("period") is None:
            item.pop("period", None)
        identity = digest(signature(item))[:16]
        item["id"] = f"{item['class']}:{identity}"
        item["confidence"] = confidence(candidate)
        item["supersedes"] = []
        old = previous_map.get(key(item))
        if old and signature(old) != signature(item):
            item["supersedes"] = [old["id"]]
            item.pop("retro", None)
        elif old and old.get("id") == item["id"]:
            item["supersedes"] = old.get("supersedes", [])
            if old.get("supersedes"):
                item.pop("retro", None)
        if item["class"] in output["portfolio"]:
            output["portfolio"][item["class"]].append(item)
        else:
            output["findings"].append(item)
    errors = schema_errors(output)
    if errors:
        raise ValueError("invalid assembled report: " + "; ".join(errors[:5]))
    state_hash = digest([records, feedback_items, PROMPT_VERSION])
    store.write(f"audit/{as_of}-{state_hash[:16]}.json", {"as_of": as_of, "candidates": candidates})
    snapshot = {"as_of": as_of, "revision": len(history),
                "items": output["findings"] + [i for group in output["portfolio"].values() for i in group]}
    store.write(f"reports/{as_of}-{state_hash[:16]}.json", snapshot)
    return output
