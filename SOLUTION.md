# My solution

## How to run it

Use Python 3.11 or newer. Run `make setup`, configure `ANTHROPIC_API_KEY` in the invoking environment, then use `make grade`, `make sheet`, `make eval` and `make validate`. On Windows, use `./tools/tasks.ps1 -Task setup`, then the `grade`, `sheet`, `eval` and `validate` tasks. The harness implements the specified `ingest`, `report` and `feedback` commands. Run `python -m unittest discover -s tests -v` for local checks without inference. Keep the repository private and credentials outside it.

The submitted runtime uses the Anthropic SDK and Claude Sonnet 4.6. The loopback transport in `tools/local_model_server.py` is an optional development helper; its actual backend identity is included in caches. `NOVA_OFFLINE=1` rejects uncached requests. There is no mailbox connector or external database.

## What works

I chose a pipeline that separates document interpretation from enforceable rules. Ingestion retains immutable emails and page-labelled PDF/Excel text in citation order, with local OCR for scanned pages. Persistent state stores records, semantic indexes, feedback, source-bound calculation plans, model responses and report history. Indexing joins invoices, shipments and instruments through explicit identifiers. Audit and independent review interpret authority, precedence, effective dates and applicability; Python controls arithmetic and action safety.

I use named numeric inputs tied to exact held source tokens. Decimal expressions round contractual line amounts before conversion and totals. Citations must resolve to the original document and page; unsupported values fail validation. Exact citation recovery is confined to the same held document and cannot substitute a different amount, identifier or clause. Authority requires appropriate signed instruments or an original notice from the relevant Schedule 1 person, rather than a model's unsupported assertion.

Feedback distinguishes standing scoped facts from one-off decisions. Scope filters fail closed when identifiers are missing. Report revisions preserve stable identities and `supersedes` history; received-date cutoffs prevent later evidence from leaking into earlier reports. Autonomous actions additionally require complete evidence, applicable authority, a held recipient, no overlapping conflict and combined recovery below USD 5,000. Missing documents produce requests or holds rather than guessed rates. The harness reports actions; it does not send mail or execute payments.

The saved practice score is 99.19/100: all verdicts and amounts match, supervised actions match 24/24, autonomous actions 21/24, and unsafe executions are zero. Three conservative holds remain. Exam artifacts were generated through the report and sheet functions, with cached successful units reused. Verification replays every monetary plan with an independent Decimal interpreter and checks original-source totals, credits, FX, conflict ranges and action gates. The scanned invoice's I/1 identifier ambiguity is corroborated against the email, attachment, ledger and waybill; OCR citations remain verbatim.

All 115 local tests pass, with compile and diff checks passing. Tests cover retrieval ownership, normalized ledger identifiers, numerical and authority provenance, feedback isolation, revisions, SDK compatibility, provider cache separation and development transport failures. The last full semantic evaluation passed 16/19 cases: two development-stream interruptions and one null-period report-assembly failure. The affected case now passes a cache-only replay after the assembly fix; the two transport interruptions were not rerun. There are no returned-report verdict/amount mismatches in this run. Results and separate failure classification are in `evals/results.json` and `evals/results_failures.json`. Exam artifacts have independently verified totals of USD 8,165.33, including USD 85.46 on the scanned invoice; submission validation passes 9/9. AI-assisted development tools were used during implementation and review; details and logs are in [AI_USE.md](AI_USE.md).

## What doesn't

Hidden correctness is not established by public scores. Exact source and arithmetic checks do not prove that semantic extraction selected every applicable clause. Confidence weights are conservative heuristics, not calibrated probabilities. Mixed-invoice conflict coverage depends on extracted scopes; incomplete authority can cause conservative holds. Native Anthropic throughput remains unmeasured against the default report budget; development CLI timings and cached replay are not API benchmarks.

The development CLI truncated input near 192 KB. Lossless compaction preserves source pages and citations. A separate large exam unit exceeded an eight-minute timeout without partial answers; only that unit was partitioned into invoice and portfolio scopes, retaining authoritative and linked evidence. This development recovery does not add a provider-specific dependency to the submitted harness. No billable Anthropic API calls were made.

## The eval case that caught a bug

`own_autonomous_complete_sources.yaml` exposed rejection of dated signatures, punctuation in parsed sender domains and an inappropriate Schedule 1 requirement for operational pickup facts. Generic fixes have regression coverage. Revision tests also guard against lost `supersedes` history after a source-bound partial credit. Transport regressions reject empty or timeout streams, recover completed concatenated JSON and preserve legitimate null pickup citations. During exam verification, an INR plan that converted twice was sent for targeted semantic repair rather than editing the answer sheet.
