from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from app import create_app
from llm import OpenAICompatibleLLM


class ObservationDrivenLLM:
    """Small test double that selects its next action from actual run observations."""

    def __init__(self, *, wrong_amount: bool = False):
        self.wrong_amount = wrong_amount
        self.actions: list[tuple[str, Any]] = []

    def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        if "task-understanding stage" in system:
            return {
                "understanding": {
                    "company": "Company X",
                    "outcome": "Create a draft payable for its latest invoice",
                    "acceptance_criteria": ["Use the latest received invoice", "Verify the draft"],
                },
                "plan": ["Find the latest Company X invoice", "Extract its payable fields", "Create and verify a draft"],
            }

        context = json.loads(user)
        state = context["state"]
        candidates = state.get("invoice_candidates", [])
        if not candidates:
            action = {"action": "tool", "tool": "search_invoices", "arguments": {"company": "Company X"}, "reason": "Locate candidate invoices."}
        elif not state.get("selected_invoice_id"):
            latest = max(candidates, key=lambda item: item["received_at"])
            action = {"action": "tool", "tool": "read_invoice", "arguments": {"invoice_id": latest["invoice_id"]}, "reason": "Read the most recently received invoice."}
        elif not state.get("approval_payload"):
            body = state["invoice_document_text"]
            invoice_number = re.search(r"Invoice number: (\S+)", body).group(1)
            amount_match = re.search(r"Amount due: ([A-Z]{3}) ([\d,]+\.\d{2})", body)
            due_date = re.search(r"Payment due date: (\d{4}-\d{2}-\d{2})", body).group(1)
            invoice_id = state["selected_invoice_id"]
            amount = amount_match.group(2).replace(",", "")
            if self.wrong_amount:
                amount = "999.00"
            action = {
                "action": "request_approval",
                "payload": {
                    "invoice_id": invoice_id,
                    "vendor": "Company X",
                    "invoice_number": invoice_number,
                    "amount": amount,
                    "currency": amount_match.group(1),
                    "due_date": due_date,
                    "idempotency_key": f"invoice:{invoice_id}",
                },
                "reason": "Confirm the exact draft payable before writing.",
            }
        elif state.get("write_outcome_unknown"):
            action = {
                "action": "tool",
                "tool": "lookup_payable",
                "arguments": {"invoice_id": state["selected_invoice_id"]},
                "reason": "The write outcome is uncertain; reconcile it before retrying.",
                "updated_plan": [
                    "Reconcile the uncertain write by reading AP",
                    "Verify the single draft against the source invoice",
                    "Return the payable ID and evidence",
                ],
            }
        elif state.get("reconciliation_records") is not None:
            action = {"action": "complete", "summary": "The payable is present.", "reason": "The read-back shows the draft entry."}
        elif state.get("tool_history") and state["tool_history"][-1]["tool"] == "create_payable":
            action = {"action": "tool", "tool": "lookup_payable", "arguments": {"invoice_id": state["selected_invoice_id"]}, "reason": "Read back the new draft for verification."}
        else:
            action = {"action": "tool", "tool": "create_payable", "arguments": state["approval_payload"], "reason": "Write the approved draft payable."}

        observation = state.get("last_observation")
        observed_kind = None
        if observation and observation.get("result", {}).get("error"):
            observed_kind = observation["result"]["error"].get("kind")
        self.actions.append((action.get("tool", action.get("action")), observed_kind))
        return action


class InvoiceWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test.sqlite3"
        self.llm = ObservationDrivenLLM()
        self.client = TestClient(
            create_app(db_path=self.db_path, llm=self.llm, inject_commit_timeout=True)
        )

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def start_task(self) -> dict[str, Any]:
        response = self.client.post(
            "/runs",
            json={"task": "Find the latest invoice from Company X, enter the amount and due date into AP, and tell me once done."},
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_end_to_end_approval_timeout_reconciliation_and_verification(self) -> None:
        run = self.start_task()
        self.assertEqual(run["status"], "awaiting_approval")
        self.assertEqual(run["context"]["selected_invoice_id"], "inv-cx-108")
        self.assertEqual(run["approval"]["payload"]["amount"], "31250.00")
        self.assertEqual(run["approval"]["payload"]["due_date"], "2026-10-21")

        # The approval gate means no payable exists yet.
        self.assertEqual(self.client.app.state.store.get_payables_for_invoice("inv-cx-108"), [])
        approval_response = self.client.post(
            f"/runs/{run['run_id']}/approval", json={"approved": True}
        )
        self.assertEqual(approval_response.status_code, 200, approval_response.text)
        completed = approval_response.json()
        self.assertEqual(completed["status"], "completed")
        self.assertTrue(completed["result"]["evidence"]["verification"] == "passed")

        events = self.client.get(f"/runs/{run['run_id']}/events").json()
        tool_results = [event["data"] for event in events if event["kind"] == "tool_result"]
        timeout = next(item for item in tool_results if item["result"].get("error", {}).get("kind") == "timeout_outcome_unknown")
        self.assertEqual(timeout["tool"], "create_payable")
        lookup_after_timeout = [
            item for item in tool_results
            if item["tool"] == "lookup_payable" and item["arguments"]["invoice_id"] == "inv-cx-108"
        ]
        self.assertTrue(lookup_after_timeout)
        self.assertTrue(any(observed == "timeout_outcome_unknown" and tool == "lookup_payable" for tool, observed in self.llm.actions))
        plan_updates = [event["data"] for event in events if event["kind"] == "plan_updated"]
        self.assertTrue(plan_updates)
        self.assertIn("Reconcile the uncertain write by reading AP", plan_updates[0]["current"])
        verification = next(event["data"] for event in events if event["kind"] == "verification")
        self.assertTrue(verification["passed"], verification)
        self.assertEqual(len(self.client.app.state.store.get_payables_for_invoice("inv-cx-108")), 1)

    def test_rejection_never_writes(self) -> None:
        run = self.start_task()
        response = self.client.post(f"/runs/{run['run_id']}/approval", json={"approved": False})
        self.assertEqual(response.json()["status"], "approval_rejected")
        self.assertEqual(self.client.app.state.store.get_payables_for_invoice("inv-cx-108"), [])

    def test_verifier_blocks_completion_when_approved_data_is_wrong(self) -> None:
        bad_llm = ObservationDrivenLLM(wrong_amount=True)
        client = TestClient(
            create_app(
                db_path=self.db_path.parent / "bad.sqlite3",
                llm=bad_llm,
                inject_commit_timeout=False,
            )
        )
        try:
            response = client.post(
                "/runs",
                json={"task": "Find the latest invoice from Company X and enter it into AP."},
            )
            self.assertEqual(response.status_code, 200, response.text)
            pending = response.json()
            written = client.post(
                f"/runs/{pending['run_id']}/approval", json={"approved": True}
            ).json()
            self.assertEqual(written["status"], "failed")
            verification = next(
                event["data"]
                for event in client.get(f"/runs/{pending['run_id']}/events").json()
                if event["kind"] == "verification"
            )
            self.assertFalse(verification["passed"])
            self.assertTrue(any("amount" in message for message in verification["problems"]))
        finally:
            client.close()

    def test_health_and_demo_page(self) -> None:
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        page = self.client.get("/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Approval required", page.text)
        self.assertIn("Task Completed", page.text)
        self.assertIn("Task Failed", page.text)
        self.assertIn("Payment was NOT initiated", page.text)
        self.assertIn("Human approval received", page.text)
        self.assertIn("Timeout reconciled", page.text)
        self.assertIn("Source matched payable", page.text)
        self.assertIn("Exactly one payable", page.text)
        self.assertIn("Independently verified", page.text)
        self.assertIn("View Execution Details", page.text)
        self.assertIn('id="runView"', page.text)
        self.assertIn('id="events"', page.text)


class ProviderAdapterTests(unittest.TestCase):
    def test_parses_json_wrapped_in_markdown_or_surrounding_prose(self) -> None:
        class FakeResponse:
            def __init__(self, content: str):
                self.content = content

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": self.content}}]}
                ).encode()

        expected = {
            "action": "tool",
            "tool": "search_invoices",
            "arguments": {"company": "Company X"},
        }
        responses = [
            'Here is the next action:\n```json\n'
            '{"action":"tool","tool":"search_invoices",'
            '"arguments":{"company":"Company X"}}\n```\n',
            '  The requested JSON is below.\n'
            '{"action":"tool","tool":"search_invoices",'
            '"arguments":{"company":"Company X"}}\nThat is the next step.  ',
        ]

        for content in responses:
            with self.subTest(content=content[:32]), patch(
                "llm.urlopen", return_value=FakeResponse(content)
            ):
                result = OpenAICompatibleLLM(
                    base_url="https://api.example.test/v1",
                    model="gemini-compatible-model",
                    api_key="test-only-secret",
                ).complete_json(system="Return JSON", user="Choose the next action")
                self.assertEqual(result, expected)

    def test_openai_compatible_endpoint_configuration_and_json_response(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return json.dumps(
                    {"choices": [{"message": {"content": '{"next":"lookup_payable"}'}}]}
                ).encode()

        client = OpenAICompatibleLLM(
            base_url="https://api.example.test/v1/",
            model="example-model",
            api_key="test-only-secret",
        )
        with patch("llm.urlopen", return_value=FakeResponse()) as send:
            result = client.complete_json(system="Return JSON", user="Choose the next action")

        request = send.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.example.test/v1/chat/completions")
        self.assertEqual(payload["model"], "example-model")
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(request.get_header("Authorization"), "Bearer test-only-secret")
        self.assertEqual(result, {"next": "lookup_payable"})

    def test_existing_chat_completions_endpoint_is_not_duplicated(self) -> None:
        client = OpenAICompatibleLLM(base_url="http://localhost:1234/v1/chat/completions")
        self.assertEqual(client.endpoint, "http://localhost:1234/v1/chat/completions")


if __name__ == "__main__":
    unittest.main()
