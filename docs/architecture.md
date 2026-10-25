# Architecture

## Stack

| Layer | Choice | Why |
|---|---|---|
| Language | Python 3.13 | Common, easy to hire for, good for data work in later phases |
| Web | FastAPI, server-rendered Jinja2 pages, small vanilla JS | Fast pages on old PCs and phones, no build step |
| Database | SQLAlchemy 2 + Alembic migrations | Same code on PostgreSQL, MySQL, SQL Server; SQLite for demos |
| Sign-in | Google Workspace (OpenID Connect) + app-only logins | Uses the company accounts |
| Charts | SVG drawn on the server | No external chart service, works offline and prints |
| AI | Pluggable provider (Anthropic, OpenAI-compatible, Gemini, none) | No lock-in, can switch to an in-house model |
| Package | One Docker image | Runs on any host |

## The data model (blueprint core keys)

Five keys join everything: **Employee ID**, **Org Unit code**, **Position
ID**, **Job**, **Grade**. Every change to a person's job is an **Employment
Event** with an effective date (hire, transfer, promotion, group change,
exit), so any past month can be rebuilt and turnover is counted from events,
not from overwritten fields.

```
OrgUnit (tree, Department Head) ─┬─ Position (seats, budget, critical) ── Employee
                                 └─ Calendar / Shift                       │
Employee ── EmploymentEvent (history)                                      │
         ── EmployeeRestricted (ID card, passport, permit: HR only)        │
         ── DailyAttendance ◄── ScanPunch ◄── ScanImport (timelog file)    │
         ── OTRequest ──► PayBatchLine ──► PayBatch ──► CSV to Bplus       │
ApprovalRequest ── ApprovalStep   (one engine for every request type)
FormCapture ── CaptureLine        (AI-read paper forms, HR review)
DataIssue (rules), AuditLog, User, UserScope, Setting
```

## One approval engine

Every request (OT, new employee, correction, transfer, promotion, exit) is an
`ApprovalRequest` with steps. A step is either "Department Head of unit X"
(found on the org chart, walking up when a unit has no head) or a role (HR,
GM). Rules built in:

- Nobody approves their own request or a request about themselves.
- When the submitter is the Department Head, that step is skipped.
- A rejection needs a reason.
- Each request type can run code after a step and when it closes (for
  example OT becomes "approved" after the Department Head, and the employee
  record changes only when the last step approves).

New request types in later phases (leave, training, exit clearance) reuse it.

## OT and pay flow

```
Supervisor enters OT ─► live checks ─► Department Head ─► (GM override if over 36h)
   ─► face scan import computes covered minutes ─► HR verifies
   ─► pay batch (draft) ─► second HR person checks ─► CSV ─► Bplus ─► imported
   ─► Bplus totals by unit come back ─► reconciliation
```

The OT rules live in `app/services/timecalc.py` and run the same way for the
web form, the AI-read paper form and the scan check.

## Code layout

```
app/
  models/      tables (core, time, system)
  services/    business rules: people, ot, timecalc, facescan, paybatch,
               approvals, health, analytics, ai/
  web/         one router per menu area
  templates/   pages
  i18n/        th.json, my.json (English text is the key)
  seed/        reference data and SAMPLE demo data
migrations/    Alembic
tests/         rules and end-to-end flows
```

## Next phases on the same base

| Phase | Adds | Reuses |
|---|---|---|
| 2 HR Development | Leave, exit clearance and interview, ILUO skill matrix, JD, onboarding, probation, training/TNA | Employee, Position, approval engine, data health |
| 3 HR Management | Appraisal/KPI, 9-box, ER cases, pulse survey, headcount budget | Events, org tree, dashboards |
| 4 Strategic HR | Succession for critical positions, HRBP pack per department | Position critical/min backups, skill matrix |
| 5 Predictive | Attrition and absence risk flags | Exit reason codes, OT, absence, tenure history |
