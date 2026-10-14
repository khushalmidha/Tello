"""Development transport parsing; no model inference or network."""
import unittest
import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch
from tools.local_model_server import response_from_events, valid_envelope, structured_schema, audit_prompt, compact_documents, compact_large_payload, run_agy


class ProxyTests(unittest.TestCase):
    def test_cli_timeout_rejects_a_misleading_successful_partial_result(self):
        request = {'system': [{'text': 'audit'}], 'messages': [{'content': '{}'}]}
        completed = subprocess.CompletedProcess([], 0, json.dumps({'event': 'result', 'result': {
            'status': 'SUCCESS', 'response': '{"items":[]}'}}) + '\n',
            '[agy] print timeout after 8m0s with turn in progress; returning partial output')
        with tempfile.TemporaryDirectory() as folder, patch('tools.local_model_server.ROOT', Path(folder)), \
                patch('tools.local_model_server.subprocess.run', return_value=completed) as launch:
            with self.assertRaisesRegex(RuntimeError, 'timeout'):
                run_agy('never-executed', request)
            self.assertEqual(len(list(Path(folder, 'out/local_proxy_traces').glob('*.json'))), 1)
            self.assertNotIn('--effort', launch.call_args.args[0])  # Sonnet CLI rejects this option.

    def test_line_interning_is_lossless_across_pages_and_newlines(self):
        common = 'Shared contract sentence with exact punctuation, spaces and whitespace.\r\n'
        data = {"documents": [{"doc": "doc-A", "received_at": "2045-01-01", "pages": [
                {"page": 1, "ocr": False, "text": common + 'Currency ₹\n' + common},
                {"page": 2, "ocr": True, "text": common + ' Last line  '}]}]}
        compact = compact_documents(data)
        self.assertEqual(len(compact['documents']), len(data['documents']))
        for actual, original in zip(compact['documents'][0]['pages'], data['documents'][0]['pages']):
            recovered = ''.join(compact['shared_lines'][part] if isinstance(part, int) else part for part in actual['text_parts'])
            self.assertEqual(recovered, original['text'])
            self.assertEqual(actual['page'], original['page'])
            self.assertEqual(actual['ocr'], original['ocr'])
        self.assertIn('text', data['documents'][0]['pages'][0])

    def test_structured_compaction_preserves_metadata_draft_and_unknown_fields(self):
        quote = 'A repeated exact evidence quote with punctuation and a material rate USD 12.34.'
        data = {'documents': [{'doc': 'held.pdf', 'received_at': '2045-01-01', 'pages': [
                 {'page': 1, 'ocr': True, 'text': quote + '\n' + quote + '\n'},
                 {'page': 2, 'ocr': False, 'text': 'Extra information', 'extra': 'retain me'}]},
                 {'doc': 'extra.pdf', 'received_at': '2045-01-02', 'pages': [], 'issuer': 'retain issuer'}],
                'proposed_audit': {'items': [{'evidence': [{'quote': quote}, {'quote': quote}]}]}}
        compact = compact_large_payload(data)
        doc, received, pages = compact['documents'][0]
        self.assertEqual((doc, received), ('held.pdf', '2045-01-01'))
        self.assertEqual(pages[0][:2], [1, True])
        text = ''.join(compact['shared_lines'][v] if isinstance(v, int) else v for v in pages[0][2])
        self.assertEqual(text, data['documents'][0]['pages'][0]['text'])
        self.assertEqual(pages[1]['extra'], 'retain me')
        self.assertEqual(compact['documents'][1]['issuer'], 'retain issuer')
        for citation in compact['proposed_audit']['items'][0]['evidence']:
            self.assertEqual(compact['shared_strings'][citation['quote']['$text']], quote)
    def test_compact_prompt_preserves_all_document_text_without_nested_encoding(self):
        data = {"documents": [{"doc": "box/invoice.pdf", "pages": [{"page": 2,
                 "text": 'Quoted "rate": 1.20\nCurrency ₹ INR; signatures remain here.'}]}],
                "target_invoice_ids": ["BILL-8"]}
        prompt, indexed = audit_prompt({"system": [{"text": "audit rules"}],
             "messages": [{"content": json.dumps(data) + "\nPrevious output invalid: retry JSON"}]})
        body = prompt.split("BEGIN UNTRUSTED INPUT\n", 1)[1].split("\nEND UNTRUSTED INPUT", 1)[0]
        self.assertEqual(json.loads(body), data)
        self.assertIn("Previous output invalid: retry JSON", prompt)
        self.assertNotIn('"role": "user"', prompt)
        self.assertFalse(indexed)

    def test_uncompressible_oversized_payload_is_rejected_before_inference(self):
        request = {"system": "audit rules", "messages": [{"content": json.dumps({
            "documents": [{"doc": "big-scan", "pages": [{"page": 1, "text": "x" * 190000}]}]})}]}
        with patch('tools.local_model_server.subprocess.run') as command:
            with self.assertRaisesRegex(RuntimeError, "no inference was attempted"):
                run_agy('unused', request)
            command.assert_not_called()

    def test_provider_schema_arrays_have_typed_items(self):
        def visit(value):
            if isinstance(value, dict):
                if value.get("type") == "array":
                    self.assertIn("items", value)
                    self.assertTrue(value["items"])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)
        for index in (False, True):
            visit(structured_schema(index))

    def test_nonshipment_portfolio_can_have_null_pickup_citation(self):
        from jsonschema import validate
        validate({"items": [{"class": "expiry", "verdict": "finding", "context": {
            "requires_pickup": False, "pickup": {"kind": "not_applicable", "date": None, "evidence": None}}}],
            "coverage": [], "portfolio_checked": ["AG-1"]}, structured_schema())

    def test_empty_success_is_rejected(self):
        with self.assertRaises(RuntimeError):
            response_from_events([{"event": "result", "result": {"status": "SUCCESS", "response": ""}}])

    def test_structured_object_is_used(self):
        result = response_from_events([{"event": "result", "result": {
            "status": "SUCCESS", "response": "", "structured_output": {"items": []}}}])
        self.assertTrue(valid_envelope(result))

    def test_actual_response_deltas_recover_empty_terminal_text(self):
        events = [{"event": "step_update", "step_update": {
            "step_type": "agent_response", "step_index": 2, "state": "ACTIVE", "text_delta": '{"items":'}},
            {"event": "step_update", "step_update": {
            "step_type": "agent_response", "step_index": 2, "state": "DONE", "text_delta": '{"items":[]}'}},
            {"event": "result", "result": {"status": "SUCCESS", "response": ""}}]
        result = response_from_events(events)
        self.assertEqual(result["response"], '{"items":[]}')
        self.assertTrue(valid_envelope(result))

    def test_concatenated_terminal_answers_recover_last_complete_assistant_json(self):
        first = '```json\n{"items":[{"class":"duplicate"}]}\n```'
        last = '```json\n{"items":[]}\n```'
        events = [{"event": "step_update", "step_update": {"step_type": "agent_response",
                   "step_index": n, "state": "DONE", "text_delta": text}}
                  for n, text in [(1, first), (3, last)]]
        events.append({"event": "result", "result": {"status": "SUCCESS", "response": first + last}})
        result = response_from_events(events)
        self.assertEqual(result['response'], last)
        self.assertTrue(valid_envelope(result))

    def test_incomplete_later_answer_does_not_recover_an_earlier_decision(self):
        events = [{"event": "step_update", "step_update": {"step_type": "agent_response",
                   "step_index": 1, "state": "DONE", "text_delta": '{"items":[]}'}},
                  {"event": "step_update", "step_update": {"step_type": "agent_response",
                   "step_index": 3, "state": "ACTIVE", "text_delta": '{"items":['}},
                  {"event": "result", "result": {"status": "SUCCESS", "response": ''}}]
        with self.assertRaises(RuntimeError):
            response_from_events(events)

    def test_thinking_and_tools_are_not_response_text(self):
        with self.assertRaises(RuntimeError):
            response_from_events([{"event": "step_update", "step_update": {
                "step_type": "tool", "text_delta": '{"items":[]}'}},
                {"event": "result", "result": {"status": "SUCCESS", "response": ""}}])

    def test_poisoned_cache_is_not_a_valid_result(self):
        for response in ("", "not JSON", "[]"):
            self.assertFalse(valid_envelope({"status": "SUCCESS", "response": response}))
