# Design

## Fact model

I retain emails as immutable received-date records, with page-labelled attachment text, original references, batch identity and OCR markers. A content-addressed semantic index records explicit invoice, shipment and contract identifiers. Exact joins retrieve history and support documents; unlinked correspondence remains available so a poorly indexed waiver is not silently lost. Proposals include deciding evidence, authority, applicability dates, calculation plans and feedback provenance. Atomic writes commit the manifest last.

## Conflicts

Semantic audit and independent review interpret per-term precedence and the instruments in force on the pickup date. Conflicting authoritative sources remain a conflict when precedence does not rank them; a weak or quoted notice does not create one. Two source-backed Decimal plans express the financial range. Code blocks autonomous actions in overlapping contract, lane, charge and date scopes, including secondary scopes on mixed invoices. Scope completeness remains an extraction limitation.

## Confidence

An inspectable weighted score combines completeness, authority, extraction quality, source agreement, effective-date coverage and ambiguity. Audit/review disagreement reduces confidence. Citation validation runs before assembly. I keep these weights explicitly heuristic: they were not fitted to public answer keys and are not claimed as probabilities. Calibration would require independent labelled cases and separate analysis of OCR and authority ambiguity.

## People in the loop

I keep action safety deterministic and fail-closed. Expiry alerts, payment reminders and credit tracking are internal actions. Supervised remedies are drafts. Combined outbound recoveries of USD 5,000 or more on an invoice or cashback key require review. Autonomous mode also checks completeness, pickup evidence when pricing needs it, conflicts, reversibility, term/fact authority and a held Schedule 1 recipient. A request for missing documents does not need an invented price, but still needs recipient authority and an exact reference. Missing gates hold the action. The harness has no sending or payment tools.

## Feedback

Feedback persists with decision time, item identity and batch. A one-off close, rejection or amount override affects only that key. Standing facts must match every scope filter and shipment date; missing values fail closed. Semantic interpretation can apply learned rates, waivers, authority and precedence; code checks claimed feedback identifiers and records `learned_at`. Later signed instruments can supersede learned terms.

## New documents

Only evidence received by end-of-day UTC enters a report. Re-ingestion is idempotent; changed content invalidates indexing. Reports re-evaluate available invoices, credits, amendments and feedback. Semantic amount/verdict identities produce stable identifiers; changed identities reference predecessors through `supersedes`, and repeated reports retain that lineage. Retrospective findings without a predecessor carry `retro`. Code removes expired alerts and findings outside their governing windows.

## Cost

I separate semantic inference from Decimal arithmetic so numerical results can be replayed without model calls. Exact numeric lexemes and evidence bind inputs; expressions implement line rounding and a single final FX conversion. Models propose formulas rather than authoritative totals.

Cache keys include endpoint fingerprint, requested and actual backend model, prompt/schema versions and request content; credentials are excluded. This prevents a development response from satisfying a native-provider request. Direct API work supports up to twelve concurrent units; local proxy execution is sequential. A preliminary budget assumption of USD 3/15 per million input/output tokens gives USD 43.50 for twelve million input and half a million output tokens per 1,000 documents, before caching. This is a planning assumption, not measured usage or a current pricing guarantee. Native throughput remains unmeasured.

## What I'd do next

Benchmark native Claude against the default 600-second budget, calibrate confidence on unseen mailboxes and improve field-level OCR confidence. Semantic clause selection and complete conflict scopes remain the main correctness limits. Development transport failures were addressed through lossless compaction, completed-stream recovery and targeted partitioning; final evidence remains traceable to original pages. Dependency-aware re-audit could reduce repeated work after new documents. No hidden-test performance is claimed.
