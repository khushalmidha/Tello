"""Development-only Anthropic-compatible proxy to the user's Antigravity subscription.

Never used by the submission unless explicitly selected with ANTHROPIC_BASE_URL.
    python tools/local_model_server.py --agy <path/to/agy> --port 8765
In a separate terminal set ANTHROPIC_BASE_URL=http://127.0.0.1:8765 and
ANTHROPIC_API_KEY=local-development (not a real secret). Requests use agy, never api.anthropic.com.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LIMIT = threading.Semaphore(4)


def run_agy(agy, request, timeout=480, model="claude-sonnet-4-6"):
    system = request.get("system", "")
    if isinstance(system, list):
        system = "\n".join(block.get("text", "") for block in system)
    prompt = ("Perform a data-only audit. Use NO tools, do not inspect files, and return exactly the requested JSON.\n"
              "BEGIN AUDIT RULES\n" + system + "\nEND AUDIT RULES\nBEGIN UNTRUSTED INPUT\n" +
              json.dumps(request.get("messages", []), ensure_ascii=True) + "\nEND UNTRUSTED INPUT")
    # stdin avoids Windows command-line length limits and shell interpretation of document text.
    with tempfile.TemporaryDirectory(prefix="nova-agy-") as directory:
        command = [agy, "--input-format", "stream-json",
                   "--model", model, "--output-format", "stream-json", "--mode", "plan",
                   "--print-timeout", f"{timeout}s"]
        message = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=True) + "\n"
        completed = subprocess.run(command, input=message, capture_output=True, text=True,
                                   encoding="utf-8", timeout=timeout + 15, cwd=directory)
    events = [json.loads(line) for line in completed.stdout.splitlines() if line.startswith("{")]
    envelopes = [event["result"] for event in events if event.get("event") == "result"]
    envelope = envelopes[-1] if envelopes else {}
    if completed.returncode or envelope.get("status") != "SUCCESS":
        raise RuntimeError(envelope.get("error") or "Antigravity did not return a successful response")
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
                if cached.exists():
                    envelope = json.loads(cached.read_text(encoding="utf-8"))
                else:
                    with LIMIT:
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
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
