from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from agent import Agent
from llm import OpenAICompatibleLLM
from simulator import InvoiceTools
from store import Store


BASE_DIR = Path(__file__).resolve().parent


class CreateRunRequest(BaseModel):
    task: str = Field(min_length=8, max_length=1200)


class ApprovalRequest(BaseModel):
    approved: bool


def create_app(
    *,
    db_path: str | Path | None = None,
    llm: Any | None = None,
    inject_commit_timeout: bool | None = None,
) -> FastAPI:
    database_path = db_path or os.getenv("INVOICE_WORKER_DB", str(BASE_DIR / "runtime" / "app.sqlite3"))
    store = Store(database_path)
    model = llm or OpenAICompatibleLLM()
    if inject_commit_timeout is None:
        inject_commit_timeout = os.getenv("SIMULATE_COMMIT_TIMEOUT", "true").strip().lower() not in {
            "0", "false", "no", "off"
        }
    tools = InvoiceTools(store, inject_commit_timeout=inject_commit_timeout)
    agent = Agent(store, tools, model)

    application = FastAPI(
        title="CentrAlign Simulated Invoice Worker",
        description="A bounded, approval-gated invoice task worker with a simulated AP system.",
        version="0.1.0",
    )
    application.state.store = store
    application.state.agent = agent

    @application.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return INDEX_HTML

    @application.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post("/runs")
    def create_run(request: CreateRunRequest) -> dict[str, Any]:
        task = request.task.strip()
        if not task:
            raise HTTPException(status_code=422, detail="Task must not be blank")
        run_id = str(uuid.uuid4())
        store.create_run(run_id, task)
        run = agent.start(run_id)
        return _run_view(run)

    @application.get("/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, Any]:
        run = store.get_run(run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return _run_view(run)

    @application.get("/runs/{run_id}/events")
    def get_events(run_id: str) -> list[dict[str, Any]]:
        if store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        return store.list_events(run_id)

    @application.post("/runs/{run_id}/approval")
    def approve_run(run_id: str, request: ApprovalRequest) -> dict[str, Any]:
        if store.get_run(run_id) is None:
            raise HTTPException(status_code=404, detail="Run not found")
        try:
            run = agent.approve(run_id, request.approved)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return _run_view(run)

    return application


def _run_view(run: dict[str, Any]) -> dict[str, Any]:
    state = run["state"]
    return {
        "run_id": run["run_id"],
        "task": run["task_text"],
        "status": run["status"],
        "understanding": state.get("understanding"),
        "plan": run["plan"],
        "context": {
            "selected_invoice_id": state.get("selected_invoice_id"),
            "selected_received_at": state.get("selected_received_at"),
            "action_count": state.get("action_count", 0),
            "last_observation": state.get("last_observation"),
        },
        "approval": run["approval"],
        "result": run["result"],
    }


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Simulated Invoice Worker</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, "Segoe UI", sans-serif;
      color: #172033;
      background: #f3f6fb;
      font-synthesis: none;
    }
    * { box-sizing: border-box; }
    body { max-width: 1040px; margin: 0 auto; padding: 36px 22px 56px; }
    h1, h2, h3, p { margin-top: 0; }
    h1 { margin-bottom: 6px; font-size: clamp(1.55rem, 3vw, 2rem); letter-spacing: -.025em; }
    h2 { margin-bottom: 8px; font-size: 1.25rem; }
    h3 { margin-bottom: 10px; font-size: 1rem; }
    .muted { color: #637187; }
    .intro { margin-bottom: 24px; }
    .eyebrow { display: block; margin-bottom: 7px; color: #61718a; font-size: .72rem; font-weight: 750; letter-spacing: .09em; text-transform: uppercase; }
    .panel, .result-card, .approval-card, .progress-card, .failure-card {
      margin: 16px 0; padding: 22px; border: 1px solid #dce4ef; border-radius: 14px;
      background: #fff; box-shadow: 0 8px 22px rgba(24, 44, 77, .045);
    }
    .task-label { display: block; margin-bottom: 9px; }
    textarea { display: block; width: 100%; min-height: 94px; padding: 13px 14px; resize: vertical; border: 1px solid #c9d3e0; border-radius: 9px; color: #172033; background: #fff; font: inherit; line-height: 1.5; }
    textarea:focus, button:focus-visible, summary:focus-visible { outline: 3px solid #b8d0fb; outline-offset: 2px; }
    button { min-height: 42px; margin: 12px 8px 0 0; padding: 10px 16px; border: 0; border-radius: 8px; cursor: pointer; color: #fff; background: #2458a6; font: inherit; font-weight: 700; }
    button.secondary { color: #34435a; background: #e8edf4; }
    button:disabled { opacity: .55; cursor: wait; }
    #message { min-height: 1.35em; margin: 12px 0 0; }
    .error { color: #a12626; }
    .result-card { border-color: #c9e5d7; }
    .result-head, .approval-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }
    .result-title { margin-bottom: 4px; font-size: 1.42rem; }
    .result-company { margin-bottom: 18px; color: #45566f; font-weight: 650; }
    .status-chip { flex: 0 0 auto; padding: 6px 10px; border-radius: 999px; color: #176341; background: #e4f5eb; font-size: .72rem; font-weight: 800; letter-spacing: .06em; text-transform: uppercase; }
    .draft-callout { display: flex; align-items: flex-start; gap: 10px; margin: 0 0 18px; padding: 12px 14px; border: 1px solid #eed9a2; border-radius: 9px; color: #634710; background: #fff9e9; line-height: 1.45; }
    .draft-callout strong { display: block; color: #553b08; }
    .field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(155px, 1fr)); gap: 10px; margin: 0 0 19px; }
    .field { min-width: 0; padding: 12px 13px; border: 1px solid #e5eaf1; border-radius: 9px; background: #fbfcfe; }
    .field-label { display: block; margin-bottom: 6px; color: #6b788c; font-size: .75rem; font-weight: 700; }
    .field-value { display: block; overflow-wrap: anywhere; color: #172033; font-size: .96rem; font-weight: 700; }
    .checks-title { margin: 0 0 10px; color: #526178; font-size: .78rem; font-weight: 750; letter-spacing: .06em; text-transform: uppercase; }
    .check-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 9px 14px; }
    .check { display: flex; align-items: flex-start; gap: 9px; color: #33445a; font-size: .9rem; line-height: 1.35; }
    .check-mark { display: inline-grid; flex: 0 0 19px; width: 19px; height: 19px; place-items: center; border-radius: 50%; color: #176341; background: #e4f5eb; font-size: .78rem; font-weight: 800; }
    .check.not-passed .check-mark { color: #8b5b11; background: #fff0cf; }
    .check.unknown .check-mark { color: #58667b; background: #e9edf3; }
    .approval-card { border: 2px solid #e9c66d; background: #fffdf7; }
    .approval-card .field-grid { margin-top: 16px; }
    .approval-note { color: #6c5b32; line-height: 1.45; }
    .approval-hash { margin: 13px 0 0; color: #69778b; font-size: .82rem; overflow-wrap: anywhere; }
    .failure-card { border-color: #f0caca; background: #fffafa; }
    .failure-card .error-title { color: #a12626; }
    .error-message { margin-bottom: 0; color: #693333; line-height: 1.5; overflow-wrap: anywhere; }
    .progress-card { border-color: #d5e1f2; }
    .progress-meta { display: flex; flex-wrap: wrap; gap: 8px 20px; margin: 12px 0 0; color: #637187; font-size: .9rem; }
    .plan-list { margin: 14px 0 0; padding-left: 21px; color: #3c4b61; }
    .plan-list li { padding: 3px 0; line-height: 1.4; }
    details.execution-details { margin: 16px 0; border: 1px solid #dce4ef; border-radius: 12px; background: #fff; }
    details.execution-details > summary, .nested-details > summary { padding: 15px 18px; cursor: pointer; color: #244d88; font-weight: 750; }
    details[open].execution-details > summary { border-bottom: 1px solid #e5eaf1; }
    .details-content { padding: 16px 18px 18px; }
    .details-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 380px), 1fr)); gap: 16px; }
    .details-grid h3 { margin-bottom: 7px; color: #4a5b73; }
    pre { max-height: 430px; margin: 0; padding: 13px; overflow: auto; border: 1px solid #e5eaf1; border-radius: 8px; background: #f7f9fc; color: #243248; white-space: pre-wrap; overflow-wrap: anywhere; font: 12px/1.5 Consolas, "Courier New", monospace; }
    .nested-details { margin-top: 14px; border-top: 1px solid #eee4c9; }
    .nested-details > summary { padding: 12px 0 6px; color: #695522; font-size: .88rem; }
    .nested-details pre { margin: 8px 0 12px; }
    [hidden] { display: none !important; }
    @media (max-width: 600px) {
      body { padding: 24px 14px 40px; }
      .panel, .result-card, .approval-card, .progress-card, .failure-card { padding: 17px; border-radius: 11px; }
      .result-head, .approval-head { flex-direction: column; gap: 8px; }
      .status-chip { align-self: flex-start; }
      .field-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    }
  </style>
</head>
<body>
  <header class="intro">
    <span class="eyebrow">CentrAlign · simulated AP environment</span>
    <h1>Invoice task worker</h1>
    <p class="muted">Draft entry only · approval required before writing · payment is never initiated</p>
  </header>
  <section class="panel" aria-labelledby="taskHeading">
    <label id="taskHeading" class="task-label" for="task"><strong>What should the worker do?</strong></label>
    <textarea id="task">Find the latest invoice from Company X, enter its amount and due date into the AP system, and tell me once done.</textarea>
    <button id="start">Start task</button>
    <p id="message" class="muted" role="status" aria-live="polite"></p>
  </section>
  <section id="approval" class="approval-card" aria-labelledby="approvalHeading" hidden>
    <div class="approval-head">
      <div>
        <span class="eyebrow">Human checkpoint</span>
        <h2 id="approvalHeading">Approval required</h2>
        <p class="approval-note">Review this exact draft payable. No payable is written until you approve it.</p>
      </div>
      <span class="status-chip" style="color:#795815;background:#fff0cf">Awaiting review</span>
    </div>
    <div id="approvalPayload"></div>
    <button id="approve">Approve draft entry</button>
    <button id="reject" class="secondary">Reject</button>
  </section>
  <section id="runSection" aria-live="polite" hidden>
    <div id="stateView"></div>
    <details id="executionDetails" class="execution-details">
      <summary>View Execution Details</summary>
      <div class="details-content">
        <div class="details-grid">
          <div><h3>Run JSON</h3><pre id="runView"></pre></div>
          <div><h3>Evidence and event log</h3><pre id="events"></pre></div>
        </div>
      </div>
    </details>
  </section>
  <script>
    let activeRunId = null;
    let timer = null;
    const terminalStatuses = ['completed', 'failed', 'approval_rejected', 'needs_clarification'];
    const setMessage = (text, isError = false) => {
      const node = document.getElementById('message');
      node.textContent = text;
      node.className = isError ? 'error' : 'muted';
    };
    const escapeHtml = value => String(value ?? '').replace(/[&<>"']/g, char => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
    })[char]);
    const displayValue = value => value === null || value === undefined || value === '' ? '—' : String(value);
    function field(label, value) {
      return `<div class="field"><span class="field-label">${escapeHtml(label)}</span><span class="field-value">${escapeHtml(displayValue(value))}</span></div>`;
    }
    function formatAmount(amount, currency) {
      const numericAmount = Number(amount);
      const code = String(currency || '').toUpperCase();
      if (!Number.isFinite(numericAmount)) return `${displayValue(amount)}${code ? ` ${code}` : ''}`;
      try {
        if (code) return new Intl.NumberFormat('en-IN', {
          style: 'currency', currency: code, minimumFractionDigits: 2, maximumFractionDigits: 2
        }).format(numericAmount);
      } catch (_error) { /* Fall back to the numeric value and currency code. */ }
      return `${numericAmount.toLocaleString('en-IN', {minimumFractionDigits: 2, maximumFractionDigits: 2})}${code ? ` ${code}` : ''}`;
    }
    function formatDate(value) {
      const match = String(value || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
      if (!match) return displayValue(value);
      const date = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3])));
      return new Intl.DateTimeFormat('en-GB', {day: '2-digit', month: 'short', year: 'numeric', timeZone: 'UTC'}).format(date);
    }
    function checkItem(label, passed) {
      const className = passed === true ? '' : (passed === false ? 'not-passed' : 'unknown');
      const symbol = passed === true ? '✓' : (passed === false ? '!' : '·');
      const suffix = passed === true ? '' : (passed === false ? ' — not confirmed' : ' — unavailable');
      return `<div class="check ${className}"><span class="check-mark" aria-hidden="true">${symbol}</span><span>${escapeHtml(label + suffix)}</span></div>`;
    }
    function verificationItems(run, events, result) {
      const verificationEvent = [...events].reverse().find(event => event.kind === 'verification');
      const verification = verificationEvent?.data || null;
      const checks = verification?.checks || {};
      const humanApproval = checks.human_approval ?? (run.approval?.approved === true);
      const items = [checkItem('Human approval received', humanApproval)];
      const timeoutIndex = events.findIndex(event =>
        event.kind === 'tool_result' && event.data?.result?.error?.kind === 'timeout_outcome_unknown'
      );
      if (timeoutIndex >= 0) {
        const sourceInvoiceId = result.invoice?.invoice_id || result.payable?.source_invoice_id;
        const reconciled = events.slice(timeoutIndex + 1).some(event => {
          if (event.kind !== 'tool_result' || event.data?.tool !== 'lookup_payable' || event.data?.result?.ok !== true) return false;
          const records = event.data.result.payables;
          return Array.isArray(records) && records.some(record => record.source_invoice_id === sourceInvoiceId);
        });
        items.push(checkItem(reconciled ? 'Timeout reconciled' : 'Timeout recovery completed', reconciled));
      }
      items.push(checkItem('Source matched payable', checks.payable_matches_source));
      items.push(checkItem('Exactly one payable', checks.exactly_one_payable));
      items.push(checkItem('Independently verified', verification?.passed === true || result.evidence?.verification === 'passed'));
      return items.join('');
    }
    function renderCompleted(run, events) {
      const result = run.result || {};
      const invoice = result.invoice || {};
      const payable = result.payable || {};
      const vendor = payable.vendor || run.understanding?.company;
      const invoiceNumber = invoice.invoice_number || payable.invoice_number;
      const invoiceId = invoice.invoice_id || payable.source_invoice_id;
      const currency = payable.currency;
      const amount = formatAmount(payable.amount, currency);
      const payableStatus = payable.status ? payable.status.charAt(0).toUpperCase() + payable.status.slice(1) : '—';
      document.getElementById('stateView').innerHTML = `
        <article class="result-card">
          <div class="result-head">
            <div><span class="eyebrow">Verified task result</span><h2 class="result-title">✅ Task Completed</h2></div>
            <span class="status-chip">Completed</span>
          </div>
          <p class="result-company">${escapeHtml(displayValue(vendor))}${invoiceNumber ? ` <span aria-hidden="true">·</span> Invoice ${escapeHtml(invoiceNumber)}` : ''}</p>
          <div class="draft-callout"><span aria-hidden="true">▣</span><div><strong>This is a DRAFT payable.</strong> Payment was NOT initiated.</div></div>
          <div class="field-grid">
            ${field('Amount', amount)}
            ${field('Currency', currency)}
            ${field('Due date', formatDate(payable.due_date))}
            ${field('Invoice ID', invoiceId)}
            ${field('Payable ID', payable.payable_id)}
            ${field('Payable status', payableStatus)}
          </div>
          <p class="checks-title">Completion checks</p>
          <div class="check-grid">${verificationItems(run, events, result)}</div>
        </article>`;
    }
    function renderApproval(run) {
      const approval = run.approval || {};
      const payload = approval.payload || {};
      const approvalDetails = {
        ...payload,
        source_received_at: approval.source_received_at,
        source_document_text: approval.source_document_text,
        approval_sha256: approval.payload_sha256
      };
      document.getElementById('approvalPayload').innerHTML = `
        <div class="field-grid">
          ${field('Company', payload.vendor)}
          ${field('Invoice', payload.invoice_number)}
          ${field('Invoice ID', payload.invoice_id)}
          ${field('Amount', formatAmount(payload.amount, payload.currency))}
          ${field('Currency', payload.currency)}
          ${field('Due date', formatDate(payload.due_date))}
        </div>
        <details class="nested-details"><summary>Show exact approval payload and source details</summary><pre>${escapeHtml(JSON.stringify(approvalDetails, null, 2))}</pre></details>`;
    }
    function renderProgress(run) {
      const status = run.status === 'awaiting_approval' ? 'Awaiting your approval' : 'Task in progress';
      const actionCount = Number(run.context?.action_count || 0);
      const lastTool = run.context?.last_observation?.tool;
      const plan = Array.isArray(run.plan) ? run.plan : [];
      document.getElementById('stateView').innerHTML = `
        <article class="progress-card">
          <span class="eyebrow">Worker activity</span>
          <h2>${escapeHtml(status)}</h2>
          <p class="muted">The worker is following its plan and will pause for approval before writing a payable.</p>
          <div class="progress-meta"><span>Actions observed: <strong>${actionCount}</strong></span>${lastTool ? `<span>Last tool: <strong>${escapeHtml(lastTool)}</strong></span>` : ''}</div>
          ${plan.length ? `<h3 style="margin:16px 0 6px">Current plan</h3><ol class="plan-list">${plan.map(step => `<li>${escapeHtml(step)}</li>`).join('')}</ol>` : ''}
        </article>`;
    }
    function renderState(run, events) {
      const view = document.getElementById('stateView');
      const result = run.result || {};
      if (run.status === 'completed') {
        renderCompleted(run, events);
      } else if (run.status === 'failed') {
        const message = result.error?.message || result.message || 'The task could not be completed.';
        view.innerHTML = `<article class="failure-card"><span class="eyebrow">Run ended with an error</span><h2 class="error-title">❌ Task Failed</h2><p class="error-message">${escapeHtml(message)}</p></article>`;
      } else if (run.status === 'needs_clarification') {
        view.innerHTML = `<article class="progress-card"><span class="eyebrow">Input needed</span><h2>Task needs clarification</h2><p>${escapeHtml(result.question || result.message || 'More information is needed to continue.')}</p></article>`;
      } else if (run.status === 'approval_rejected') {
        view.innerHTML = '<article class="progress-card"><span class="eyebrow">Run ended</span><h2>Approval declined</h2><p class="muted">The draft was not approved, so this run did not create a payable.</p></article>';
      } else {
        renderProgress(run);
      }
    }
    async function request(url, options = {}) {
      const response = await fetch(url, options);
      const value = await response.json();
      if (!response.ok) throw new Error(value.detail || response.statusText);
      return value;
    }
    async function refresh() {
      if (!activeRunId) return;
      try {
        const [run, events] = await Promise.all([
          request(`/runs/${activeRunId}`), request(`/runs/${activeRunId}/events`)
        ]);
        document.getElementById('runSection').hidden = false;
        document.getElementById('runView').textContent = JSON.stringify(run, null, 2);
        document.getElementById('events').textContent = events.map(event =>
          `${event.timestamp}  ${event.kind}\n${JSON.stringify(event.data, null, 2)}`
        ).join('\n\n');
        renderState(run, events);
        const waiting = run.status === 'awaiting_approval' && run.approval && !run.approval.approved;
        document.getElementById('approval').hidden = !waiting;
        if (waiting) {
          renderApproval(run);
          setMessage('Review the draft and approve or reject it.');
        } else if (run.status === 'completed') {
          setMessage('The draft payable was verified; no payment was initiated.');
        } else if (run.status === 'failed') {
          setMessage(run.result?.error?.message || run.result?.message || 'The task could not be completed.', true);
        } else if (run.status === 'needs_clarification') {
          setMessage(run.result?.question || 'More information is needed.');
        } else if (run.status === 'approval_rejected') {
          setMessage('Approval was declined. No draft was created.');
        } else {
          setMessage('The worker is processing the task.');
        }
        if (terminalStatuses.includes(run.status) && timer) { clearInterval(timer); timer = null; }
      } catch (error) { setMessage(error.message, true); }
    }
    document.getElementById('start').addEventListener('click', async () => {
      const button = document.getElementById('start');
      button.disabled = true;
      setMessage('Starting the agent. A local model call may take a little while.');
      try {
        const run = await request('/runs', {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({task: document.getElementById('task').value})
        });
        activeRunId = run.run_id;
        await refresh();
        if (!terminalStatuses.includes(run.status)) timer = setInterval(refresh, 1200);
      } catch (error) { setMessage(error.message, true); }
      finally { button.disabled = false; }
    });
    async function decideApproval(approved) {
      if (!activeRunId) return;
      const buttons = [document.getElementById('approve'), document.getElementById('reject')];
      buttons.forEach(button => { button.disabled = true; });
      try {
        const updatedRun = await request(`/runs/${activeRunId}/approval`, {
          method: 'POST', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({approved})
        });
        if (timer) { clearInterval(timer); timer = null; }
        await refresh();
        if (!terminalStatuses.includes(updatedRun.status)) timer = setInterval(refresh, 1200);
      } catch (error) { setMessage(error.message, true); }
      finally { buttons.forEach(button => { button.disabled = false; }); }
    }
    document.getElementById('approve').addEventListener('click', () => decideApproval(true));
    document.getElementById('reject').addEventListener('click', () => decideApproval(false));
  </script>
</body>
</html>"""


app = create_app()
