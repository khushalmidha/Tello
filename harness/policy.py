"""Finding keys, exact feedback scope, confidence, and unattended-action gates."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
import re

from money import decimal


def invoice_id(value: str) -> str:
    return "".join(c for c in str(value).upper() if not c.isspace() and c not in "#:")


def invoice_in_text(identifier: str, text: str) -> bool:
    """Match canonical identity without deleting identifier boundaries or altering citations.

    Hyphens remain meaningful: AB-12 and A-B12 never become equal.
    """
    wanted = invoice_id(identifier)
    if not wanted:
        return False
    ignored = r"[\s#:]*"
    pattern = r"(?<![\w-])" + ignored.join(re.escape(c) for c in wanted) + r"(?![\w-])"
    return any(invoice_id(match.group()) == wanted for match in re.finditer(pattern, text, re.IGNORECASE))


def key(item: dict) -> tuple:
    kind = item["class"]
    if kind == "cashback":
        return kind, item["contract_id"], item["period"], item["credit_type"]
    if kind == "expiry":
        return kind, item["contract_id"]
    return kind, invoice_id(item["invoice_id"])


def comparable(value):
    return str(value).strip().casefold()


def scope_matches(feedback: dict, item: dict) -> bool:
    """All filters must match. Unknown filter values fail closed, never broaden a decision."""
    scope = feedback.get("scope", {})
    if scope.get("kind") == "one_off":
        if key(feedback["finding_key"]) != key(item):
            return False
    elif scope.get("kind") != "standing":
        return False
    context = item.get("context", {})
    for field, expected in scope.get("applies_to", {}).items():
        actual = item.get(field, context.get(field))
        if actual is None or comparable(actual) != comparable(expected):
            return False
    if scope.get("kind") == "standing":
        relevant_date = context.get("pickup", {}).get("date") or context.get("invoice_date")
        if not relevant_date:
            return False
        if scope.get("valid_from") and relevant_date < scope["valid_from"]:
            return False
        if scope.get("valid_to") and relevant_date > scope["valid_to"]:
            return False
    return True


def open_feedback(records: list[dict], as_of: str) -> list[dict]:
    end = as_of + "T23:59:59.999999+00:00"
    from datetime import datetime, timezone
    unique = {}
    for record in records:
        decided = datetime.fromisoformat(record["decided_at"].replace("Z", "+00:00"))
        if decided.tzinfo is None:
            raise ValueError("feedback decided_at must include a timezone")
        if decided.astimezone(timezone.utc) > datetime.fromisoformat(end):
            continue
        for item in record.get("items", []):
            unique[item["item_id"]] = dict(item, decided_at=decided.astimezone(timezone.utc).isoformat(), batch=record["after_batch"])
    return sorted(unique.values(), key=lambda item: (item["decided_at"], item["item_id"]))


def feedback_effects(candidates: list[dict], records: list[dict]) -> list[dict]:
    result = []
    by_id = {record["item_id"]: record for record in records}
    for item in candidates:
        discard = False
        # Provenance is assigned only from held, applicable feedback below; never accept
        # a model-written learned_at as proof of a human's authority.
        item.pop("learned_at", None)
        claimed = item.pop("feedback_ids", [])
        learned = []
        for record in records:
            same_key = key(record["finding_key"]) == key(item)
            decision = record["decision"]
            if same_key and decision in ("close", "reject"):
                discard = True
            if same_key and decision == "override_amount":
                from money import usd
                item["amount_usd"] = usd(record["fact"]["value"]["amount_usd"])
                item["verdict"] = "finding"
                learned.append(record)
        if discard:
            # An exact-key close/reject is effective from the decision time even if the
            # standing fact bundled with it covers a different shipment-date range.
            continue
        for fid in claimed:
            record = by_id.get(fid)
            if record is None or not scope_matches(record, item):
                raise ValueError(f"feedback {fid!r} is outside scope for {key(item)}")
            learned.append(record)
        if learned:
            record = sorted(learned, key=lambda fb: (fb["decided_at"], fb["item_id"]))[-1]
            item["learned_at"] = {"feedback_item_id": record["item_id"], "batch": record["batch"]}
        result.append(item)
    return result


def conflicts_overlap(left: dict, right: dict) -> bool:
    def scopes(item):
        context = item.get("context", {})
        return [dict(context, **scope) for scope in context.get("scopes", [])] or [context]
    return any(scope_overlap(a, b) for a in scopes(left) for b in scopes(right))


def scope_overlap(a: dict, b: dict) -> bool:
    if not a.get("contract_id") or a.get("contract_id") != b.get("contract_id"):
        return False
    for field in ("lane", "charge"):
        if a.get(field) and b.get(field) and comparable(a[field]) != comparable(b[field]):
            return False
    # Rate-period labels differ across instruments ("P1" vs "September notice").
    # Explicit effective dates decide overlap; compare labels only when dates are unavailable.
    dated = (a.get("valid_from") or a.get("valid_to")) and (b.get("valid_from") or b.get("valid_to"))
    if not dated and a.get("period") and b.get("period") and a["period"] != b["period"]:
        return False
    a_start, a_end = a.get("valid_from") or "0001-01-01", a.get("valid_to") or "9999-12-31"
    b_start, b_end = b.get("valid_from") or "0001-01-01", b.get("valid_to") or "9999-12-31"
    return a_start <= b_end and b_start <= a_end


def confidence(item: dict) -> float:
    """A conservative, inspectable quality score; calibration requires held-out labelled runs."""
    signals = item.get("confidence_signals", {})
    extraction = max(0., min(1., float(signals.get("extraction", .7))))
    agreement = max(0., min(1., float(signals.get("agreement", .7))))
    ambiguity = max(0., min(1., float(signals.get("ambiguity", .3))))
    # Completeness for a cannot_determine verdict means the missing-doc diagnosis is well evidenced.
    score = .20 + .16 * bool(signals.get("complete")) + .18 * bool(signals.get("authoritative"))
    score += .16 * extraction + .14 * agreement + .12 * bool(signals.get("effective"))
    score -= .20 * ambiguity
    if not item.get("review_agreed", False):
        score -= .10
    return round(max(.05, min(.98, score)), 3)


def actions(candidates: list[dict], mode: str, as_of: str, citation_valid, authority_valid=None) -> None:
    totals = {}
    for item in candidates:
        if item["verdict"] != "finding" or item["class"] in ("expiry", "late_fee_risk"):
            continue
        group = key(item) if item["class"] == "cashback" else invoice_id(item["invoice_id"])
        totals[group] = totals.get(group, Decimal(0)) + decimal(item.get("amount_usd", "0"))
    conflicts = [item for item in candidates if item["verdict"] == "conflict"]
    for item in candidates:
        verdict, kind = item["verdict"], item["class"]
        context = item.get("context", {})
        if verdict == "no_finding":
            item["action"] = "none"
            continue
        if verdict == "conflict":
            item["action"] = "escalate"
            continue
        if verdict == "finding" and kind in ("expiry", "late_fee_risk"):
            item["action"] = "internal:alert" if kind == "expiry" else "internal:payment_reminder"
            continue
        if verdict == "finding" and kind == "cashback" and context.get("credit_due") and context["credit_due"] > as_of:
            item["action"] = "internal:track_credit"
            continue
        remedy = "request_document" if verdict == "cannot_determine" else "claim_credit" if kind == "cashback" else "dispute"
        group = key(item) if kind == "cashback" else invoice_id(item.get("invoice_id", ""))
        under_threshold = totals.get(group, Decimal(0)) < Decimal("5000.00")
        if mode == "supervised":
            item["action"] = f"draft:{remedy}" if under_threshold else "human_review"
            continue
        if remedy == "request_document":
            proof = authority_valid is not None and authority_valid(item, remedy)
            checks = {"evidence_complete": proof and bool(item.get("evidence")) and
                      all(citation_valid(ev) for ev in item["evidence"]),
                      "no_conflict": not any(conflicts_overlap(item, conflict) for conflict in conflicts),
                      "under_threshold": under_threshold, "reversible": True, "authority": proof}
            item["safety_checks"] = checks
            item["action"] = f"execute:{remedy}" if all(checks.values()) else "human_review"
            continue
        terms = context.get("terms", [])
        authority = bool(terms) and all(term.get("authority") in ("signed", "schedule1", "feedback") and
                    (citation_valid(term.get("evidence")) or
                     (term.get("authority") == "feedback" and item.get("learned_at", {}).get("feedback_item_id") == term.get("feedback_item_id")))
                    for term in terms)
        facts = context.get("facts", [])
        authority = authority and bool(facts) and all(citation_valid(fact) for fact in facts)
        authority = authority and item.get("confidence_signals", {}).get("authoritative") is True
        recipient = context.get("recipient", {})
        recipient_evidence = recipient.get("evidence", {})
        address = recipient.get("address", "")
        recipient_ok = bool(address) and "@" in address and address.casefold() in recipient_evidence.get("quote", "").casefold()
        recipient_ok = recipient_ok and citation_valid(recipient_evidence)
        # A Schedule 1 entry must be cited from a held signed instrument, not an invoice or email body.
        recipient_ok = recipient_ok and "#" in recipient_evidence.get("doc", "") and any(
            (term.get("authority") == "signed" and term.get("evidence", {}).get("doc") == recipient_evidence.get("doc")) or
            (term.get("authority") == "schedule1" and term.get("schedule_evidence", {}).get("doc") == recipient_evidence.get("doc")
             and term.get("signature_evidence", {}).get("doc") == recipient_evidence.get("doc"))
            for term in terms)
        pickup = context.get("pickup", {})
        requires_pickup = kind == "overcharge" or context.get("requires_pickup") is not False
        pickup_ok = not requires_pickup or (
            pickup.get("kind") in ("tracking", "carrier_email") and citation_valid(pickup.get("evidence")))
        for scope in context.get("scopes", []):
            if kind == "overcharge" or scope.get("requires_pickup", context.get("requires_pickup")) is not False:
                source = scope.get("pickup", {})
                pickup_ok = pickup_ok and source.get("kind") in ("tracking", "carrier_email") and citation_valid(source.get("evidence"))
        complete = verdict == "finding" and bool(item.get("evidence")) and all(citation_valid(ev) for ev in item["evidence"])
        complete = complete and item.get("confidence_signals", {}).get("complete") is True
        effective = item.get("confidence_signals", {}).get("effective") is True
        no_conflict = not any(conflicts_overlap(item, conflict) for conflict in conflicts)
        checks = {"evidence_complete": complete and pickup_ok and effective, "no_conflict": no_conflict,
                  "under_threshold": under_threshold, "reversible": remedy in ("dispute", "claim_credit", "request_document"),
                  "authority": authority and recipient_ok and authority_valid is not None and authority_valid(item, remedy)}
        item["safety_checks"] = checks
        item["action"] = f"execute:{remedy}" if all(checks.values()) else "human_review"
