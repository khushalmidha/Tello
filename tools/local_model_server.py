"""Development-only Anthropic-compatible proxy to the user's Antigravity subscription.

Never used by the submission unless explicitly selected with ANTHROPIC_BASE_URL.
    python tools/local_model_server.py --agy <path/to/agy> --port 8765
In a separate terminal set ANTHROPIC_BASE_URL=http://127.0.0.1:8765 and
ANTHROPIC_API_KEY=local-development (not a real secret). Also set NOVA_BACKEND_MODEL to
the actual --model selected here; it separates development backend caches. Requests use
agy, never api.anthropic.com.
"""
from __future__ import annotations

import argparse
import copy
from collections import Counter
import hashlib
import json
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIMIT = threading.Semaphore(1)
TRANSPORT_VERSION = "data-only-2026-10-14.3"


def compact_documents(data):
    """Lossless line interning, including whitespace/newlines, for large payloads."""
    documents = data.get("documents", [])
    counts = Counter(line for doc in documents for page in doc["pages"]
                     for line in page["text"].splitlines(keepends=True))
    shared = [line for line, count in counts.items() if count > 1 and len(line) > 16]
    if not shared:
        return data
    indexes = {line: index for index, line in enumerate(shared)}
    compact = copy.deepcopy(data)
    compact["shared_lines"] = shared
    for document in compact["documents"]:
        for page in document["pages"]:
            parts = []
            for line in page.pop("text").splitlines(keepends=True):
                if line in indexes:
                    parts.append(indexes[line])
                elif parts and isinstance(parts[-1], str):
                    parts[-1] += line
                else:
                    parts.append(line)
            page["text_parts"] = parts
    return compact


def compact_large_payload(data):
    compact = compact_documents(data)
    if compact is data:
        compact = copy.deepcopy(data)
    # Positional records remove repeated field names, not business content.
    # Unknown fields keep the original dictionary instead of being dropped.
    if 'shared_lines' in compact:
        for document in compact['documents']:
            document['pages'] = [[page['page'], page['ocr'], page['text_parts']]
                if set(page) == {'page', 'ocr', 'text_parts'} else page for page in document['pages']]
        compact['documents'] = [[doc['doc'], doc['received_at'], doc['pages']]
            if set(doc) == {'doc', 'received_at', 'pages'} else doc for doc in compact['documents']]
    counts = Counter()
    def collect(value):
        if isinstance(value, str):
            counts[value] += 1
        elif isinstance(value, dict):
            for child in value.values(): collect(child)
        elif isinstance(value, list):
            for child in value: collect(child)
    collect(compact.get('proposed_audit', {}))
    strings = [value for value, count in counts.items() if count > 1 and len(value) > 35]
    indexes = {value: index for index, value in enumerate(strings)}
    def intern(value):
        if isinstance(value, str): return {'$text': indexes[value]} if value in indexes else value
        if isinstance(value, dict): return {key: intern(child) for key, child in value.items()}
        if isinstance(value, list): return [intern(child) for child in value]
        return value
    if strings:
        compact['proposed_audit'] = intern(compact['proposed_audit'])
        compact['shared_strings'] = strings
    return compact


def audit_prompt(request):
    """Decode the Anthropic content once; never double-escape the document corpus."""
    system = request.get("system", "")
    if isinstance(system, list):
        system = "\n".join(block.get("text", "") for block in system)
    parts = ["Perform a data-only audit. Return the requested JSON; do not use business, filesystem or terminal tools.",
             "BEGIN AUDIT RULES", system, "END AUDIT RULES"]
    indexed = False
    for message in request.get("messages", []):
        content = message["content"]
        data, end = json.JSONDecoder().raw_decode(content.lstrip())
        indexed = indexed or "emails" in data
        if "documents" in data and len(content.encode("utf-8")) + len(system.encode("utf-8")) > 60000:
            data = compact_large_payload(data)
            parts.append("Lossless input encoding: each page's text_parts is its EXACT original text in order. "
                         "Concatenate string parts verbatim; an integer n means shared_lines[n], including its newline. "
                         "A document array is [doc, received_at, pages]; a page array is [page, ocr, text_parts]. "
                         "In proposed_audit, an object with only $text:n means the literal string shared_strings[n]. "
                         "All documents, fields and page numbers are retained. Reconstruct exact quotes from these parts. "
                         "Output normal full JSON objects and strings, not these transport encodings.")
        # Compact JSON retains every document, exact page text, identifier and date.
        parts.extend(["BEGIN UNTRUSTED INPUT", json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                      "END UNTRUSTED INPUT", content.lstrip()[end:]])
    parts.append(f"Output budget: approximately {request.get('max_tokens', 14000)} tokens; use concise JSON.")
    parts.append("Transport protocol exception: use Antigravity's finish tool solely to return the requested JSON "
                 "when the platform requires it. No other tools or delegation are authorized.")
    return "\n".join(parts), indexed


def structured_schema(index=False):
    text = {"type": "string"}
    optional_text = {"anyOf": [text, {"type": "null"}]}
    def obj(fields, required=()):
        return {"type": "object", "properties": fields, "required": list(required)}
    def array(items):
        return {"type": "array", "items": items}
    citation = obj({"doc": text, "page": {"type": "integer"}, "quote": text}, ("doc", "page", "quote"))
    inputs = array(obj({"name": text, "value": text, "source_text": text, "evidence": citation},
                       ("name", "value", "source_text", "evidence")))
    steps = array(obj({"name": text, "expression": text, "evidence": array(citation)}, ("name", "expression", "evidence")))
    plan = obj({"currency": text, "fx": optional_text, "inputs": inputs, "steps": steps, "amount": text,
                "dates": array(obj({"value": text, "evidence": citation}, ("value", "evidence")))},
               ("currency", "inputs", "steps", "amount"))
    pickup = obj({"date": optional_text, "kind": text, "evidence": {"anyOf": [citation, {"type": "null"}]},
                  "carrier_evidence": {"anyOf": [citation, {"type": "null"}]},
                  "signature_evidence": {"anyOf": [citation, {"type": "null"}]}})
    scope = {name: optional_text for name in ("contract_id", "lane", "charge", "period", "period_end", "valid_from",
                                             "valid_to", "invoice_date", "person", "category", "credit_due")}
    scope.update(requires_pickup={"type": "boolean"}, pickup=pickup)
    context = dict(scope, scopes=array(obj(scope)), facts=array(citation),
        recipient=obj({"address": text, "category": text, "evidence": citation, "signature_evidence": citation}),
        terms=array(obj({"authority": text, "category": optional_text, "evidence": citation,
                         "signature_evidence": citation, "schedule_evidence": citation, "feedback_item_id": text})))
    fields = {name: optional_text for name in ("invoice_id", "contract_id", "period", "credit_type", "deadline")}
    fields.update({"class": text, "verdict": text, "evidence": array(citation), "calculation": plan,
        "range_calculations": array(plan), "context": obj(context), "feedback_ids": array(text), "retro": {"type": "boolean"},
        "confidence_signals": obj({name: {"type": "boolean"} if name in ("complete", "authoritative", "effective") else {"type": "number"}
                                   for name in ("complete", "authoritative", "effective", "extraction", "agreement", "ambiguity")}),
        "missing_doc": {"anyOf": [obj({"name": text, "holder": obj({"person": text, "party": text, "contact": text})}), {"type": "null"}]},
        "risk_principal": obj({"currency": text, "fx": optional_text, "inputs": inputs, "steps": steps,
                               "open": text, "rate_percent": text, "evidence": array(citation)})})
    if index:
        record = obj({"email": text, "global": {"type": "boolean"}, **{name: array(text) for name in
                     ("contract_ids", "invoice_ids", "billed_invoice_ids", "shipment_ids", "kinds")}})
        return obj({"records": array(record)}, ("records",))
    return obj({"items": array(obj(fields, ("class", "verdict"))),
                "coverage": array(obj({"invoice_id": text, "checked": array(text), "reason": text}, ("invoice_id", "checked"))),
                "portfolio_checked": array(text)}, ("items", "coverage", "portfolio_checked"))


def response_from_events(events):
    envelopes = [event["result"] for event in events if event.get("event") == "result"]
    envelope = dict(envelopes[-1]) if envelopes else {}
    if envelope.get("status") != "SUCCESS":
        raise RuntimeError(envelope.get("error") or "Antigravity did not return a successful response")
    if isinstance(envelope.get("structured_output"), dict):
        envelope["response"] = json.dumps(envelope["structured_output"], ensure_ascii=True)
    # The CLI may concatenate several fenced assistant answers after repeatedly
    # asking for finish. A nonempty terminal string can therefore still be invalid.
    if not valid_envelope(envelope):
        responses = {}
        completed = set()
        for event in events:
            step = event.get("step_update", {})
            if step.get("step_type") != "agent_response" or not step.get("text_delta"):
                continue
            index = step.get("step_index", 0)
            old, delta = responses.get(index, ""), step["text_delta"]
            responses[index] = delta if step.get("state") == "DONE" and delta.startswith(old) else old + delta
            if step.get("state") == "DONE":
                completed.add(index)
        latest = max(responses) if responses else None
        envelope["response"] = responses[latest] if latest in completed else ""
        if envelope["response"]:
            envelope["completion_source"] = "last_completed_assistant_step"
    if not envelope["response"].strip():
        raise RuntimeError("Antigravity completed without assistant response text; result was not cached")
    return envelope


def valid_envelope(envelope):
    raw = envelope.get("response", "").strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
    try:
        return envelope.get("status") == "SUCCESS" and isinstance(json.loads(raw), dict)
    except ValueError:
        return False


def run_agy(agy, request, timeout=1050, model="claude-sonnet-4-6"):
    prompt, indexed = audit_prompt(request)
    if len(prompt.encode("utf-8")) > 186000:
        raise RuntimeError("Compacted prompt exceeds the safe Antigravity input size; no inference was attempted")
    # stdin avoids Windows command-line length limits and shell interpretation of document text.
    with tempfile.TemporaryDirectory(prefix="nova-agy-") as directory:
        schema = structured_schema(index=indexed)
        command = [agy, "--input-format", "stream-json", "--json-schema", json.dumps(schema),
                   "--model", model, "--output-format", "stream-json", "--mode", "plan",
                   "--print-timeout", f"{timeout}s"]
        message = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=True) + "\n"
        try:
            completed = subprocess.run(command, input=message, capture_output=True, text=True,
                                       encoding="utf-8", timeout=timeout + 15, cwd=directory)
        except subprocess.TimeoutExpired as exc:
            decode = lambda value: value.decode("utf-8", "replace") if isinstance(value, bytes) else value or ""
            completed = subprocess.CompletedProcess(command, 1, decode(exc.stdout),
                         decode(exc.stderr) + "\nAntigravity subprocess timed out before completion")
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.lstrip().startswith("{")]
    trace_root = ROOT / "out" / "local_proxy_traces"
    trace_root.mkdir(parents=True, exist_ok=True)
    trace_key = hashlib.sha256(json.dumps([model, request], sort_keys=True).encode()).hexdigest()
    (trace_root / (trace_key + "-" + str(time.time_ns()) + ".json")).write_text(json.dumps({
        "model": model, "transport_version": TRANSPORT_VERSION, "request": request,
        "cli_timeout_seconds": timeout,
        "prompt_characters": len(prompt), "events": events, "stderr": completed.stderr,
        "returncode": completed.returncode}), encoding="utf-8")
    if completed.returncode:
        raise RuntimeError("Antigravity command failed; inspect the saved development trace")
    if "[agy] print timeout" in completed.stderr:
        raise RuntimeError("Antigravity turn exceeded its timeout; partial output was not cached")
    envelope = response_from_events(events)
    if not valid_envelope(envelope):
        raise RuntimeError("Antigravity response is not a JSON object; result was not cached")
    return envelope


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agy", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default="claude-sonnet-4-6", help="local validation model; submission still uses Claude")
    parser.add_argument("--cache-dir", default="out/local_proxy_cache", help="development-only response cache")
    args = parser.parse_args()
    cache_dir = Path(args.cache_dir).resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                if self.path != "/v1/messages":
                    self.send_error(404)
                    return
                length = int(self.headers.get("Content-Length", "0"))
                if length > 10_000_000:
                    self.send_error(413)
                    return
                request = json.loads(self.rfile.read(length))
                cache_key = hashlib.sha256(json.dumps([args.model, request], sort_keys=True).encode()).hexdigest()
                cached = cache_dir / (cache_key + ".json")
                envelope = json.loads(cached.read_text(encoding="utf-8")) if cached.exists() else {}
                if not valid_envelope(envelope):
                    with LIMIT:
                        envelope = json.loads(cached.read_text(encoding="utf-8")) if cached.exists() else {}
                        if not valid_envelope(envelope):
                            if cached.exists():
                                failed = cache_dir / "failed"
                                failed.mkdir(exist_ok=True)
                                cached.replace(failed / cached.name)
                            envelope = run_agy(args.agy, request, model=args.model)
                            temporary = cached.with_suffix(".tmp")
                            temporary.write_text(json.dumps(envelope, ensure_ascii=True), encoding="utf-8")
                            temporary.replace(cached)
                text = envelope["response"].strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                result = {"id": "msg_local_" + envelope["conversation_id"], "type": "message", "role": "assistant",
                          "model": args.model, "content": [{"type": "text", "text": text}],
                          "stop_reason": "end_turn", "stop_sequence": None,
                          "usage": {"input_tokens": envelope.get("usage", {}).get("input_tokens", 0),
                                    "output_tokens": envelope.get("usage", {}).get("output_tokens", 0)}}
                code = 200
            except Exception as exc:
                result = {"type": "error", "error": {"type": "api_error", "message": str(exc)}}
                code = 503
            payload = json.dumps(result, ensure_ascii=True).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            try:
                self.end_headers()
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                # The result is already safely cached even if an older client timed out.
                pass

        def log_message(self, format, *arguments):
            # Never log headers, credentials, request payloads, or document content.
            print(format % arguments, flush=True)

    print(f"Local subscription proxy on http://127.0.0.1:{args.port}; external Anthropic calls disabled here.", flush=True)
    print(f"Client configuration: NOVA_BACKEND_MODEL={args.model}", flush=True)
    print(f"Grader endpoint: http://127.0.0.1:{args.port}/#nova_backend_model={args.model}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
