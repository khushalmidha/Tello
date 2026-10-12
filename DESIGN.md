# Design

## Fact model

Each email is an immutable received-date record with page-labelled attachment text, original document references, batch identity and an OCR marker. A content-addressed semantic index stores explicit contract, billed-invoice and shipment identifiers and document kinds. Exact identifier joins retrieve history and support records; unlinked emails remain available so a poorly indexed waiver is not silently lost. Audit proposals retain deciding evidence, authority, effective dates, calculation plans and feedback provenance. Atomic files commit the manifest last.

## Conflicts

The audit stage reads per-term precedence and the instruments in force on the pickup date. Independent review checks the same sources again. Disagreement is a conflict only when both sources have authority and precedence does not rank them; weak or quoted notices do not create one. A conflict has two evidence sources and two Decimal calculation plans. Code prevents autonomous execution in overlapping contract/lane/charge/date scopes, including secondary scopes on mixed invoices. Coverage of those scopes still depends on semantic extraction.

## Confidence

An inspectable weighted score uses completeness, authority, extraction quality, source agreement, effective-date coverage and ambiguity. Audit/review disagreement lowers it. Citation validation precedes report assembly. Weights are heuristic; I have not fitted them to public answer keys or claimed held-out calibration. Follow-up work would measure Brier score and AUROC on independent labelled cases, stratified by OCR and authority ambiguity.

## People in the loop

Expiry alerts, payment reminders and not-yet-due credit tracking are internal actions. Supervised remedies are drafts; USD 5,000 or more of combined outbound recoveries on an invoice or cashback key requires review. Autonomous mode also checks completeness, the pickup source when pricing requires it, scoped conflicts, reversibility, term/fact authority and a held Schedule 1 recipient. Missing-document requests therefore remain held in autonomous mode. The report describes actions; this assessment harness has no mail-sending or payment-execution tools.

## Feedback

Feedback envelopes and facts stay on disk with decision time, item id and batch. Exact-key closes/rejections remove only that key; amount overrides apply only there. Standing facts match every scope filter and shipment date; missing filter values fail closed. One-off decisions cannot affect another finding key. Semantic stages apply rate, authority, waiver and precedence facts; code verifies claimed feedback ids and writes `learned_at`. Later signed instruments can supersede learned terms.

## New documents

Reports use only emails received by end-of-day UTC. Re-ingestion is idempotent, and changed content invalidates its index. Each report re-evaluates available evidence, including previous invoices, credits, amendments and feedback. Semantic amount/verdict identities produce stable ids; changed identities cite their prior id in `supersedes`. Repeated reports preserve that lineage. A genuinely new retrospective finding has no parent and carries `retro`. Code removes expired alerts and closed-period/window violations.

## Cost

Claude Sonnet 4.6 is configured; published input/output pricing is USD 3/15 per million tokens. A preliminary estimate for 1,000 documents is 2 million indexing input tokens, 10 million combined audit/review input tokens and 0.5 million output tokens: approximately USD 43.50 before prompt caching. This is an assumption, not measured Claude usage. State logs record actual token counts and latency for measurement. Up to twelve API work units run concurrently; local proxies use four. Development subscription-proxy timings are not representative of direct API throughput.

Pricing reference: https://platform.claude.com/docs/en/models/sonnet-4-6/overview

## What I'd do next

Finish Claude-backed replay and exam verification, benchmark the 600-second budget, and inspect misses individually. Replace single-scope invoice context with per-line applicability scopes and verify Schedule 1 signatures/categories against a normalized authority graph. Add field-level OCR confidence and source-bound numeric operands, so code can check selected quantities/rates as well as arithmetic. Calibrate confidence on unseen mailboxes and incrementally re-audit only the dependency closure changed by a new document.
