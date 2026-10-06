from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Store:
    """Small SQLite persistence layer for the simulated AP system and task runs."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def connection(self):
        db = self.connect()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def initialize(self) -> None:
        with self.connection() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS invoices (
                    invoice_id TEXT PRIMARY KEY,
                    vendor TEXT NOT NULL,
                    received_at TEXT NOT NULL,
                    document_text TEXT NOT NULL,
                    truth_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS payables (
                    payable_id TEXT PRIMARY KEY,
                    source_invoice_id TEXT NOT NULL UNIQUE,
                    vendor TEXT NOT NULL,
                    invoice_number TEXT NOT NULL,
                    amount TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    due_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(source_invoice_id) REFERENCES invoices(invoice_id)
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task_text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    state_json TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    approval_json TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    data_json TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                CREATE TABLE IF NOT EXISTS injected_faults (
                    run_id TEXT PRIMARY KEY,
                    fault_name TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES runs(run_id)
                );
                """
            )
            for invoice in INVOICE_FIXTURES:
                db.execute(
                    """INSERT OR IGNORE INTO invoices
                       (invoice_id, vendor, received_at, document_text, truth_json)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        invoice["invoice_id"],
                        invoice["vendor"],
                        invoice["received_at"],
                        invoice["document_text"],
                        json.dumps(invoice["truth"]),
                    ),
                )

    def create_run(self, run_id: str, task_text: str) -> None:
        now = utc_now()
        with self.connection() as db:
            db.execute(
                """INSERT INTO runs
                   (run_id, task_text, status, state_json, plan_json, created_at, updated_at)
                   VALUES (?, ?, 'running', '{}', '[]', ?, ?)""",
                (run_id, task_text, now, now),
            )

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            return None
        return {
            "run_id": row["run_id"],
            "task_text": row["task_text"],
            "status": row["status"],
            "state": json.loads(row["state_json"]),
            "plan": json.loads(row["plan_json"]),
            "approval": json.loads(row["approval_json"]) if row["approval_json"] else None,
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def update_run(
        self,
        run_id: str,
        *,
        status: str | None = None,
        state: dict[str, Any] | None = None,
        plan: list[str] | None = None,
        approval: dict[str, Any] | None | object = ..., 
        result: dict[str, Any] | None | object = ...,
    ) -> None:
        current = self.get_run(run_id)
        if current is None:
            raise KeyError(f"Run {run_id} does not exist")
        values: dict[str, Any] = {"updated_at": utc_now()}
        if status is not None:
            values["status"] = status
        if state is not None:
            values["state_json"] = json.dumps(state)
        if plan is not None:
            values["plan_json"] = json.dumps(plan)
        if approval is not ...:
            values["approval_json"] = json.dumps(approval) if approval is not None else None
        if result is not ...:
            values["result_json"] = json.dumps(result) if result is not None else None
        assignments = ", ".join(f"{column} = ?" for column in values)
        with self.connection() as db:
            db.execute(
                f"UPDATE runs SET {assignments} WHERE run_id = ?",
                (*values.values(), run_id),
            )

    def append_event(self, run_id: str, kind: str, data: dict[str, Any]) -> dict[str, Any]:
        timestamp = utc_now()
        with self.connection() as db:
            cursor = db.execute(
                "INSERT INTO events (run_id, timestamp, kind, data_json) VALUES (?, ?, ?, ?)",
                (run_id, timestamp, kind, json.dumps(data)),
            )
            event_id = cursor.lastrowid
        return {"event_id": event_id, "timestamp": timestamp, "kind": kind, "data": data}

    def list_events(self, run_id: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT event_id, timestamp, kind, data_json FROM events WHERE run_id = ? ORDER BY event_id",
                (run_id,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "timestamp": row["timestamp"],
                "kind": row["kind"],
                "data": json.loads(row["data_json"]),
            }
            for row in rows
        ]

    def search_invoices(self, vendor: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT invoice_id, vendor, received_at FROM invoices WHERE lower(vendor) = lower(?) ORDER BY invoice_id",
                (vendor,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM invoices WHERE invoice_id = ?", (invoice_id,)).fetchone()
        if row is None:
            return None
        return {
            "invoice_id": row["invoice_id"],
            "vendor": row["vendor"],
            "received_at": row["received_at"],
            "document_text": row["document_text"],
            "truth": json.loads(row["truth_json"]),
        }

    def commit_payable_if_absent(
        self, run_id: str, payload: dict[str, str], *, inject_timeout: bool
    ) -> tuple[dict[str, Any], bool, bool]:
        with self.connection() as db:
            existing = db.execute(
                "SELECT * FROM payables WHERE source_invoice_id = ? OR idempotency_key = ?",
                (payload["invoice_id"], payload["idempotency_key"]),
            ).fetchone()
            if existing is not None:
                return dict(existing), False, False

            payable_id = f"PAY-{payload['invoice_id'].upper()}"
            db.execute(
                """INSERT INTO payables
                   (payable_id, source_invoice_id, vendor, invoice_number, amount, currency,
                    due_date, status, idempotency_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
                (
                    payable_id,
                    payload["invoice_id"],
                    payload["vendor"],
                    payload["invoice_number"],
                    payload["amount"],
                    payload["currency"],
                    payload["due_date"],
                    payload["idempotency_key"],
                    utc_now(),
                ),
            )
            inject_now = False
            if inject_timeout:
                marker = db.execute(
                    "SELECT 1 FROM injected_faults WHERE run_id = ?", (run_id,)
                ).fetchone()
                if marker is None:
                    db.execute(
                        "INSERT INTO injected_faults (run_id, fault_name) VALUES (?, ?)",
                        (run_id, "commit_then_timeout"),
                    )
                    inject_now = True
            db.commit()
            record = db.execute(
                "SELECT * FROM payables WHERE source_invoice_id = ?", (payload["invoice_id"],)
            ).fetchone()
        return dict(record), inject_now, True

    def get_payables_for_invoice(self, invoice_id: str) -> list[dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM payables WHERE source_invoice_id = ? ORDER BY created_at",
                (invoice_id,),
            ).fetchall()
        return [dict(row) for row in rows]


INVOICE_FIXTURES = [
    {
        "invoice_id": "inv-cx-104",
        "vendor": "Company X",
        "received_at": "2026-09-28T09:12:00+05:30",
        "document_text": (
            "INVOICE\nVendor: Company X\nInvoice number: CX-104\n"
            "Invoice date: 2026-09-26\nAmount due: INR 28,400.00\n"
            "Payment due date: 2026-10-20\nPlease remit by the due date."
        ),
        "truth": {
            "invoice_id": "inv-cx-104",
            "invoice_number": "CX-104",
            "vendor": "Company X",
            "amount": "28400.00",
            "currency": "INR",
            "due_date": "2026-10-20",
        },
    },
    {
        "invoice_id": "inv-cx-108",
        "vendor": "Company X",
        "received_at": "2026-10-02T11:35:00+05:30",
        "document_text": (
            "INVOICE\nVendor: Company X\nInvoice number: CX-108\n"
            "Invoice date: 2026-09-30\nAmount due: INR 31,250.00\n"
            "Payment due date: 2026-10-21\nThis invoice supersedes no prior invoice."
        ),
        "truth": {
            "invoice_id": "inv-cx-108",
            "invoice_number": "CX-108",
            "vendor": "Company X",
            "amount": "31250.00",
            "currency": "INR",
            "due_date": "2026-10-21",
        },
    },
    {
        "invoice_id": "inv-cy-203",
        "vendor": "Company Y",
        "received_at": "2026-10-03T08:00:00+05:30",
        "document_text": (
            "INVOICE\nVendor: Company Y\nInvoice number: CY-203\n"
            "Amount due: INR 9,800.00\nPayment due date: 2026-10-25"
        ),
        "truth": {
            "invoice_id": "inv-cy-203",
            "invoice_number": "CY-203",
            "vendor": "Company Y",
            "amount": "9800.00",
            "currency": "INR",
            "due_date": "2026-10-25",
        },
    },
]
