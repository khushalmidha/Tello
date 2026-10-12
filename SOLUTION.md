# My solution

## How to run it

Use Python 3.11 or newer. On Linux: `make setup`, then configure `ANTHROPIC_API_KEY` in the invoking environment; optionally configure `ANTHROPIC_BASE_URL`. Run `make grade`, `make sheet`, `make eval`, and `make validate`. Keep the repository private. Nothing needs an external database, a browser, or a mailbox connector.

On Windows, the equivalent wrapper is `./tools/tasks.ps1 setup`, then `./tools/tasks.ps1 grade`, `sheet`, `eval`, and `validate`. The harness accepts the three CLI commands exactly as specified. Run `python -m unittest discover -s tests -v` for the non-billable unit and mocked integration checks.

Local subscription validation can explicitly use `tools/local_model_server.py` and a loopback `ANTHROPIC_BASE_URL`; see that script's help. This is a development helper only. The submitted harness uses the Anthropic SDK with Claude Sonnet 4.6 and the grader's supplied environment. The helper neither calls api.anthropic.com nor buys credits. `NOVA_OFFLINE=1` makes uncached development requests fail without network access.

## What works

The harness reads PDF and Excel attachments in the grader's citation order, performs local OCR on image pages, and saves all email records, indexing, feedback, calculations, model responses and report history under the requested state directory. Semantic indexing joins invoices, shipments and contracts without carrier-specific regexes. Separate audit and review calls interpret source authority and terms; Decimal code computes amounts. Code enforces received-date cutoffs, date windows, exact feedback scopes, combined recovery thresholds, citation existence and autonomous safety checks.

The 39 initial local tests passed, including persistent revisions and mocked model-boundary tests. The worked overcharge example returned USD 196.92 in both modes, with all quotes validated and the expected draft/execute actions. The independent eval set contains 14 newly written mailboxes.

Full practice, exam and independent eval results are being verified. Development live calls have used Gemini through the owner's Antigravity subscription because its Claude quota was exhausted. Actual model names in state logs distinguish these runs. These are not claimed as Claude validation.

## What doesn't

Hidden-batch correctness cannot be established from the public mailbox, and no hidden-test score is claimed. Interpretation remains probabilistic: code checks exact citations and arithmetic, but cannot independently prove that a model chose the right clause. Confidence weights are conservative heuristics rather than an empirically calibrated probability model. Mixed invoices enumerate multiple lane/charge applicability scopes, but completeness of that enumeration depends on extraction.

Final Claude-backed practice and exam verification is still required before submission. Large mailboxes and slow endpoints need measured timing against the default 600-second report budget. Failed extraction, malformed proposals or an unavailable model exit with an error rather than manufacturing a clean report. No paid API calls were authorised during development, and none were made by the development proxy.

## The eval case that caught a bug

The mocked integration case `test_changed_amount_supersedes_and_repeat_preserves_history` caught a real persistence bug: rerunning the same revised report erased its prior `supersedes` reference. The report assembler now preserves that reference when the finding identity is unchanged. `test_sdk_arguments_match_pinned_sdk` guards the SDK signature after a local endpoint run exposed a removed `temperature` argument. Independent mailbox case `own_disputed_principal_risk.yaml` additionally checks that an USD 88.00 dispute reduces a USD 264.00 open balance to USD 176.00 before the 1% risk fee, producing USD 1.76.
