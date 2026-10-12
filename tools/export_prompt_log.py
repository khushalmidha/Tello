"""Export visible user/assistant/tool records; exclude internal system/context/reasoning records."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session")
    parser.add_argument("--out", default="ai_log/development.jsonl")
    args = parser.parse_args()
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    for line in Path(args.session).read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("type") != "response_item":
            continue
        payload = row.get("payload", {})
        kind = payload.get("type")
        if kind == "message":
            if payload.get("role") not in ("user", "assistant") or payload.get("channel") in ("analysis", "summary"):
                continue
        elif kind not in ("function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"):
            continue
        text = json.dumps(row, ensure_ascii=True)
        if kind.endswith("call_output") and "base_instructions" in text:
            row["payload"] = {"type": kind, "output": "[Internal session metadata omitted from visible conversation export]"}
            text = json.dumps(row, ensure_ascii=True)
        text = re.sub(r"sk-ant-[A-Za-z0-9_\-]{20,}", "[REDACTED_API_KEY]", text)
        entries.append(text)
    output.write_text("\n".join(entries) + "\n", encoding="utf-8")
    print(f"Exported {len(entries)} visible prompt, response and tool records to {output}")


if __name__ == "__main__":
    main()
