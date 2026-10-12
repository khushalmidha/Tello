# AI use

## Tools

OpenAI Codex with GPT-6.1-sol wrote the implementation, prompts, tests, independent mailboxes and documentation. Google Antigravity CLI provided development-only live model validation: initially Gemini 3.1 Pro (High), then Gemini 3.8 Flash (Medium). Claude Sonnet 4.6 is the submitted harness's Anthropic API model; Antigravity's Claude quota was exhausted during the initial development session. No Astra delegation was used.

## How you worked with them

The user's request was to implement the linked take-home strictly following its README and glossary, with strong coverage of unseen cases. The user subsequently forbade billable Anthropic API calls and purchases without approval, and requested use of their Antigravity subscription plus mocks. Codex read the brief, glossary, schemas, extraction and grading interfaces; implemented semantic indexing, audit/review roles and deterministic policy/math; and ran local checks. These are software roles within the harness, not separate coding subagents.

Mocked responses validate infrastructure and safety behaviour without claiming semantic accuracy. Live development responses are separately identifiable by actual-model metadata. Local runs exposed an incompatible SDK argument and lost revision lineage; both were corrected and tested. No practice answer file is read by the harness or embedded as an answer table. Practice answer files are used only by the provided grader.

## Written without AI

No newly written implementation, test, mailbox or documentation section is claimed as written without AI. The original brief, examples, grader and sample cases are the repository authors' work and remain their materials.

## Prompt log

The actual visible development conversation and tool calls are exported to [ai_log/development.jsonl](ai_log/development.jsonl). The exporter excludes internal system/context/reasoning records from the visible-chat export and redacts API-secret patterns. It retains the user's prompts, public assistant responses and tool inputs/outputs. Harness model calls also retain complete prompts and responses in each state directory's `model_log/`, with actual models and usage in `model_cache/`. Final verification logs should be copied into `ai_log/` before submission; keys must never be included.
