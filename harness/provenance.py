"""Small, conservative proposal checks, not a natural-language authority theorem prover."""
from __future__ import annotations

import ast
import re
from datetime import date
from email.utils import parseaddr

from money import calculation_values, decimal, evaluate


def calculation_provenance(plan, evidence, fields=("amount",)):
    """Every material literal enters via an exact source token; formulas reference names."""
    inputs = plan.get("inputs", [])
    if not inputs:
        raise ValueError("calculation requires source-bound inputs")
    for source in inputs:
        token = source.get("source_text", "")
        citation = source.get("evidence")
        numeric = re.fullmatch(r"(?:(?:USD|INR)\s*)?([+-]?\d[\d,]*(?:\.\d+)?)(?:\s*(?:kg|%|USD|INR))?",
                              token, re.I) if isinstance(token, str) else None
        if numeric is None:
            raise ValueError("input needs an explicit decimal source_text token")
        # Preserve the whole cited lexeme, including OCR-joined units. Its numeric
        # component must still equal the input; identifiers/arbitrary suffixes fail.
        suffix = r"(?![\w]|[.,]\d)" if token[-1].isalpha() or token[-1] == '%' else r"(?!\d|[.,]\d)"
        matches = re.finditer(r"(?<![\d.,+-])" + re.escape(token) + suffix, (citation or {}).get("quote", ""))
        # OCR may concatenate a currency code or unit with a value, but a suffix of
        # an invoice/shipment identifier is not a monetary source token.
        matched = any((not citation["quote"][:match.start()] or not citation["quote"][match.start() - 1].isalnum()
                       or re.search(r"\b(?:USD|INR)$", citation["quote"][:match.start()], re.I)) for match in matches)
        if not evidence.valid(citation) or not matched:
            raise ValueError("calculation input token is not in its cited source")
        if decimal(numeric.group(1).replace(",", "")) != decimal(source["value"]):
            raise ValueError("calculation input value differs from source token")
    date_tokens = set()
    for source in plan.get("dates", []):
        date.fromisoformat(source["value"])
        if not evidence.valid(source.get("evidence")) or source["value"] not in source["evidence"]["quote"]:
            raise ValueError("calculation date lacks source provenance")
        date_tokens.add(source["value"])
    # Only the glossary's dimensionless constants are permitted inline. Rates, weights,
    # fees, thresholds and FX must be bound even when their value equals a constant.
    expressions = [step["expression"] for step in plan.get("steps", [])]
    expressions += [plan[field] for field in fields]
    if plan.get("currency", "USD").upper() == "INR":
        expressions.append(plan["fx"])
    if any(not isinstance(expression, str) or len(expression) > 4096 for expression in expressions):
        raise ValueError("invalid calculation expression")
    dependencies = {source["name"]: {source["name"]} for source in inputs}
    date_derived = set()
    for step in plan.get("steps", []):
        tree = ast.parse(step["expression"], mode="eval")
        nodes = list(ast.walk(tree))
        dependencies[step["name"]] = set().union(*(dependencies.get(node.id, set()) for node in nodes if isinstance(node, ast.Name)))
        if any((isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "days") or
               (isinstance(node, ast.Name) and node.id in date_derived) for node in nodes):
            date_derived.add(step["name"])
    for expression in expressions:
        tree = ast.parse(expression, mode="eval")
        parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant):
                token = node.value if isinstance(node.value, str) else ast.get_source_segment(expression, node)
                parent = parents.get(node)
                if token in date_tokens:
                    if not isinstance(parent, ast.Call) or not isinstance(parent.func, ast.Name) or parent.func.id != "days":
                        raise ValueError("source dates are only allowed as days() arguments")
                    continue
                value = decimal(token)
                allowed = False
                if value == 0:
                    allowed = isinstance(parent, ast.Call) and isinstance(parent.func, ast.Name) and parent.func.id == "max"
                elif value in (30, 100) and isinstance(parent, ast.BinOp) and isinstance(parent.op, ast.Div) and parent.right is node:
                    allowed = value == 100
                    if value == 30:
                        ceiling = parents.get(parent)
                        days_based = any((isinstance(part, ast.Call) and isinstance(part.func, ast.Name) and part.func.id == "days") or
                                         (isinstance(part, ast.Name) and part.id in date_derived) for part in ast.walk(parent.left))
                        allowed = (days_based and isinstance(ceiling, ast.Call) and isinstance(ceiling.func, ast.Name)
                                   and ceiling.func.id == "ceil")
                if isinstance(node.value, bool) or not allowed:
                    raise ValueError("material calculation literal must be a source-bound input")
    values = calculation_values(plan)
    for field in fields + (("fx",) if plan.get("currency", "USD").upper() == "INR" else ()):
        expression = plan[field]
        used = set().union(*(dependencies.get(node.id, set()) for node in
            ast.walk(ast.parse(expression, mode="eval")) if isinstance(node, ast.Name)))
        if not used:
            raise ValueError(f"{field} must derive from source-bound inputs")
        evaluate(expression, values)


class AuthorityProof:
    """Require held affirmative proof and reject contradictory/quoted authority text.

    Missing or unfamiliar proof holds an outbound action for review; it never turns a
    finding into no_finding. These textual checks support, but cannot authenticate, signatures.
    """
    def __init__(self, evidence):
        self.evidence = evidence

    def text(self, citation):
        doc = self.evidence.docs.get((citation or {}).get("doc"), {})
        return "\n".join(doc.get("pages", []))

    def original(self, citation):
        if not self.evidence.valid(citation):
            return False
        text = self.text(citation)
        if "#" not in citation["doc"] and text.startswith("From:"):
            # An original reply can contain an authorized decision above a quoted
            # conversation. Only the unquoted top message can confer authority.
            boundary = re.search(r"(?im)^\s*>|^\s*On .+wrote:\s*$|^.*(?:forwarded message|begin forwarded|original message).*$", text)
            if boundary:
                from grader.extract import norm
                text = text[:boundary.start()]
                if norm(citation["quote"]) not in norm(text):
                    return False
        return not re.search(r"(?im)^\s*>|forwarded message|begin forwarded|original message|"
                             r"\bunsigned\b|\bnot\s+(?:yet\s+)?(?:signed|executed)\b|\bdraft\b", text)

    def signed(self, clause, signature):
        if not self.original(clause) or not self.original(signature):
            return False
        if "#" not in clause["doc"] or clause["doc"] != signature["doc"]:
            return False
        quote = signature["quote"]
        # A clause saying an instrument MUST be signed is not evidence it WAS signed.
        affirmative = [line for line in quote.splitlines() if not re.search(
            r"\b(?:if|only|must|may|shall|unless|required|should|will)\b|\bto be\b", line, re.I)]
        if any(re.search(r"\b(?:signed|executed)(?:\s+on\s+\d{4}-\d{2}-\d{2})?\s+(?:by\s+)?(?:both|all)\s+parties\b", line, re.I)
               for line in affirmative):
            return True
        # The quote may select just the execution sentence. Bind both signature
        # blocks to its actual held page, rather than requiring a model to repeat
        # the whole execution section in every term/contact citation.
        page = self.evidence.docs[signature["doc"]]["pages"][signature["page"] - 1]
        return bool(re.search(r"\bexecuted by (?:duly )?authori[sz]ed officers\b", "\n".join(affirmative), re.I)
                    and len(re.findall(r"\bsigned for and on behalf of\b", page, re.I)) >= 2
                    and not re.search(r"signature\s*:\s*(?:_+|\[\s*\]|blank)", page, re.I))

    def schedule(self, citation, signature, address, category=None):
        if not self.signed(citation, signature) or not address or "@" not in address:
            return False
        quote = citation["quote"].casefold()
        if address.casefold() not in re.findall(r"[\w.+-]+@[\w](?:[\w.-]*[\w])?", quote):
            return False
        page = self.evidence.docs[citation["doc"]]["pages"][citation["page"] - 1]
        from grader.extract import norm
        prefix = norm(page).split(norm(citation["quote"]), 1)[0]
        if not re.search(r"\bschedule\s*1\b", prefix + " " + quote, re.I):
            return False
        # Inspect actual page rows, not model-selected quote line breaks. One category
        # cannot transfer to another address by quoting the entire Schedule 1 table.
        for row in page.splitlines():
            if address.casefold() not in re.findall(r"[\w.+-]+@[\w](?:[\w.-]*[\w])?", row.casefold()):
                continue
            if len(re.findall(r"[\w.+-]+@[\w](?:[\w.-]*[\w])?", row)) != 1:
                continue
            if not category:
                return True
            pattern = r"[\s_-]+".join(re.escape(word) for word in category.split("_"))
            if re.search(r"\b" + pattern + r"\b", row, re.I):
                return True
        return False

    def sender(self, citation):
        text = self.text(citation)
        if not self.original(citation) or "#" in citation["doc"] or not text.startswith("From:"):
            return ""
        return parseaddr(text.splitlines()[0][5:].strip())[1]

    def pickup(self, source, carrier_contact=None):
        citation = source.get("evidence")
        if not self.original(citation) or not source.get("date") or source["date"] not in citation["quote"]:
            return False
        if not re.search(r"\bpicked[ _-]+up\b", citation["quote"], re.I):
            return False
        if re.search(r"\b(?:not|never|no|cancelled|canceled|pending|scheduled|planned|expected)\b", citation["quote"], re.I):
            return False
        if source.get("kind") == "carrier_email" or (source.get("kind") == "tracking" and "#" not in citation["doc"]):
            address = self.sender(citation)
        elif source.get("kind") == "tracking":
            # The export's enclosing held email identifies its sender. A model's
            # 'tracking' label on an arbitrary PDF is insufficient.
            if "#" not in citation["doc"] or not citation["doc"].lower().endswith(".xlsx"):
                return False
            parent = {"doc": citation["doc"].split("#", 1)[0]}
            text = self.text(parent)
            address = parseaddr(text.splitlines()[0][5:].strip())[1] if text.startswith("From:") else ""
        else:
            return False
        carrier_contact = carrier_contact or {}
        proof = source.get("carrier_evidence") or carrier_contact.get("evidence")
        signature = source.get("signature_evidence") or carrier_contact.get("signature_evidence")
        if self.schedule(proof, signature, address):
            return True
        # Schedule 1 authorizes changes to terms, not every carrier employee's facts.
        # Bind an operational sender to the issuer domain of a held signed contact;
        # this does not grant that sender waiver/rate authority.
        if not address or not self.signed(proof, signature):
            return False
        domain = address.rpartition("@")[2].casefold()
        contacts = re.findall(r"[\w.+-]+@[\w](?:[\w.-]*[\w])?", proof["quote"])
        return bool(domain) and any(contact.rpartition("@")[2].casefold() == domain and
                                    self.schedule(proof, signature, contact) for contact in contacts)

    def supports(self, item, remedy):
        context = item.get("context", {})
        if remedy == "request_document":
            missing = item.get("missing_doc") or {}
            holder = missing.get("holder") or {}
            name, person = missing.get("name"), holder.get("person") or holder.get("party")
            recipient = context.get("recipient") or {}
            if not isinstance(name, str) or not isinstance(person, str) or not recipient.get("category"):
                return False
            def reference(value):
                return re.sub(r"[\W_]+", "", value.casefold().replace("revision", "rev"))
            named = reference(name)
            if len(named) < 3 or not self.schedule(recipient.get("evidence"), recipient.get("signature_evidence"),
                    recipient.get("address"), recipient["category"]):
                return False
            if reference(person) not in reference(recipient["evidence"]["quote"]):
                return False
            # Asking a proven holder for a named source does not adjudicate its
            # unknown prices. Bind the diagnosis to an actual original reference.
            return any(self.original(citation) and re.search(re.escape(named) + (r"(?!\d)" if named[-1].isdigit() else ""),
                       reference(citation["quote"])) for citation in item.get("evidence", []))
        waiver_authority = False
        for term in context.get("terms", []):
            kind = term.get("authority")
            if kind == "signed":
                if not self.signed(term.get("evidence"), term.get("signature_evidence")):
                    return False
            elif kind == "schedule1":
                address = self.sender(term.get("evidence"))
                if re.search(r"\bwaiv(?:e[ds]?|ers?|ing)\b", (term.get("evidence") or {}).get("quote", ""), re.I) and term.get("category") != "waivers":
                    return False
                if not term.get("category") or not self.schedule(term.get("schedule_evidence"),
                        term.get("signature_evidence"), address, term["category"]):
                    return False
                waiver_authority = waiver_authority or term["category"] == "waivers"
            elif kind == "feedback":
                if not term.get("feedback_item_id") or term["feedback_item_id"] != item.get("learned_at", {}).get("feedback_item_id"):
                    return False
                waiver_authority = waiver_authority or context.get("category") == "waivers"
            else:
                return False
        if item["class"] == "waived_but_billed" and not waiver_authority:
            return False
        if not context.get("terms") or not context.get("facts") or not all(self.original(fact) for fact in context["facts"]):
            return False
        recipient = context.get("recipient", {})
        # Claim-credit recipients vary; require the category and its actual Schedule 1
        # row. Disputes have the category explicitly required by the glossary.
        category = "billing_disputes" if remedy == "dispute" else recipient.get("category")
        if remedy == "claim_credit" and category not in {"billing_disputes", "credits", "rebates", "cashback", "treasury"}:
            return False
        if not category or recipient.get("category") != category or not self.schedule(recipient.get("evidence"),
                recipient.get("signature_evidence"), recipient.get("address"), category):
            return False
        contexts = [context] + context.get("scopes", [])
        for scope in contexts:
            if item["class"] == "overcharge" or scope.get("requires_pickup", context.get("requires_pickup")) is not False:
                if not self.pickup(scope.get("pickup", {}), recipient):
                    return False
        return True
