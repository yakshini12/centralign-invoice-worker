from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Callable

from store import Store


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "search_invoices",
        "description": "Find invoice candidates for an exact vendor name. Use received_at to determine which is latest.",
        "parameters": {
            "type": "object",
            "properties": {"company": {"type": "string"}},
            "required": ["company"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_invoice",
        "description": "Read the document text for one invoice so its fields can be extracted.",
        "parameters": {
            "type": "object",
            "properties": {"invoice_id": {"type": "string"}},
            "required": ["invoice_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "create_payable",
        "description": "Create a draft payable entry. This has a side effect and requires approval of the exact payload.",
        "parameters": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "string"},
                "vendor": {"type": "string"},
                "invoice_number": {"type": "string"},
                "amount": {"type": "string"},
                "currency": {"type": "string"},
                "due_date": {"type": "string"},
                "idempotency_key": {"type": "string"},
            },
            "required": [
                "invoice_id", "vendor", "invoice_number", "amount", "currency",
                "due_date", "idempotency_key",
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "lookup_payable",
        "description": "Read the accounts-payable system for entries linked to an invoice; use to reconcile uncertain writes.",
        "parameters": {
            "type": "object",
            "properties": {"invoice_id": {"type": "string"}},
            "required": ["invoice_id"],
            "additionalProperties": False,
        },
    },
]


def normalize_payload(arguments: dict[str, Any]) -> dict[str, str]:
    required = (
        "invoice_id", "vendor", "invoice_number", "amount", "currency", "due_date",
        "idempotency_key",
    )
    if set(arguments) != set(required):
        raise ValueError(f"Expected exactly these fields: {', '.join(required)}")
    normalized = {field: str(arguments[field]).strip() for field in required}
    if any(not value for value in normalized.values()):
        raise ValueError("All payable fields must be non-empty")
    try:
        amount = Decimal(normalized["amount"].replace(",", ""))
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Amount must be a positive finite number")
        normalized["amount"] = f"{amount.quantize(Decimal('0.01')):.2f}"
    except InvalidOperation as exc:
        raise ValueError("Amount must be a valid decimal number") from exc
    try:
        normalized["due_date"] = date.fromisoformat(normalized["due_date"]).isoformat()
    except ValueError as exc:
        raise ValueError("due_date must use YYYY-MM-DD") from exc
    normalized["currency"] = normalized["currency"].upper()
    if normalized["idempotency_key"] != f"invoice:{normalized['invoice_id']}":
        raise ValueError("idempotency_key must be invoice:<invoice_id>")
    return normalized


class InvoiceTools:
    def __init__(self, store: Store, *, inject_commit_timeout: bool = True):
        self.store = store
        self.inject_commit_timeout = inject_commit_timeout
        self.handlers: dict[str, Callable[[str, dict[str, Any]], dict[str, Any]]] = {
            "search_invoices": self.search_invoices,
            "read_invoice": self.read_invoice,
            "create_payable": self.create_payable,
            "lookup_payable": self.lookup_payable,
        }

    def execute(self, run_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self.handlers.get(tool_name)
        if handler is None:
            return {"ok": False, "error": {"kind": "unknown_tool", "message": "Tool is not allowed"}}
        try:
            result = handler(run_id, arguments)
        except (KeyError, TypeError, ValueError) as exc:
            result = {"ok": False, "error": {"kind": "invalid_input", "message": str(exc)}}
        return result

    def search_invoices(self, _run_id: str, args: dict[str, Any]) -> dict[str, Any]:
        company = args.get("company")
        if not isinstance(company, str) or not company.strip():
            raise ValueError("company must be a non-empty string")
        return {"ok": True, "invoices": self.store.search_invoices(company.strip())}

    def read_invoice(self, _run_id: str, args: dict[str, Any]) -> dict[str, Any]:
        invoice_id = args.get("invoice_id")
        if not isinstance(invoice_id, str) or not invoice_id:
            raise ValueError("invoice_id must be a non-empty string")
        invoice = self.store.get_invoice(invoice_id)
        if invoice is None:
            return {"ok": False, "error": {"kind": "not_found", "message": "Invoice not found"}}
        return {
            "ok": True,
            "invoice_id": invoice["invoice_id"],
            "vendor": invoice["vendor"],
            "received_at": invoice["received_at"],
            "document_text": invoice["document_text"],
        }

    def create_payable(self, run_id: str, args: dict[str, Any]) -> dict[str, Any]:
        payload = normalize_payload(args)
        invoice = self.store.get_invoice(payload["invoice_id"])
        if invoice is None:
            return {"ok": False, "error": {"kind": "not_found", "message": "Source invoice not found"}}
        truth = invoice["truth"]
        if payload["vendor"].casefold() != truth["vendor"].casefold():
            return {"ok": False, "error": {"kind": "source_mismatch", "message": "Vendor does not match the source invoice"}}
        if payload["invoice_number"] != truth["invoice_number"]:
            return {"ok": False, "error": {"kind": "source_mismatch", "message": "Invoice number does not match the source invoice"}}
        record, timed_out, created = self.store.commit_payable_if_absent(
            run_id, payload, inject_timeout=self.inject_commit_timeout
        )
        if timed_out:
            return {
                "ok": False,
                "error": {
                    "kind": "timeout_outcome_unknown",
                    "message": "Simulated timeout after commit; the payable may already exist. Reconcile before retrying.",
                },
            }
        return {"ok": True, "created": created, "payable": record}

    def lookup_payable(self, _run_id: str, args: dict[str, Any]) -> dict[str, Any]:
        invoice_id = args.get("invoice_id")
        if not isinstance(invoice_id, str) or not invoice_id:
            raise ValueError("invoice_id must be a non-empty string")
        records = self.store.get_payables_for_invoice(invoice_id)
        return {"ok": True, "invoice_id": invoice_id, "count": len(records), "payables": records}
