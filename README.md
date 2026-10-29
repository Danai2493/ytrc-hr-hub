# YTRC HR Hub

One web app for HR work and HR monitoring: employee master, org chart and
positions, OT requests and approvals, face scan check, pay input to Bplus 7.4,
AI reading of paper forms, data health checks and dashboards. Screens in Thai,
English and Myanmar.

This is Phase 1 (HR Foundation) of the 5-level HR blueprint. Later phases
(leave, exit, skills, training, appraisal, succession, predictive flags) plug
into the same data model and approval engine.

## What it does today

| Area | What people do |
|---|---|
| People | Search, view, create and correct employee records (a second HR person checks). Transfers, promotions, exits as dated events with history. Restricted data (ID card, passport, work permit) visible to HR only and every view logged. |
| Org & positions | Org chart, Department Heads, budgeted seats, vacancies, critical positions. |
| OT | Supervisor enters OT like form FM-HRM-25 with live checks (36h weekly limit, under-18 rule, holiday calendar, overlap, shift hours). Department Head approves. GM override for the weekly limit is logged. |
| Face scan | Import the timelog file. Daily attendance, late, stayed after shift. Each OT line is checked against the scans and short lines are paid only for covered time. |
| Pay input | HR verifies OT, builds a batch per pay cycle, a second HR person checks it, the CSV goes into Bplus. Totals by unit come back from Bplus for reconciliation. Salaries and bank data stay in Bplus. |
| Paper forms (AI) | Photograph a paper OT form or work permit. AI reads it, every line goes through the same checks as the web form, HR fixes the unclear cells and accepts. |
| Data health | 17 rules (ID format, missing fields, juvenile workers, permits expiring, OT without request, unknown scan IDs...). Issues have owners and close themselves when fixed. |
| Dashboards | People and turnover, OT, attendance, monthly HR KPI pack (print to PDF). No Power BI needed. |
| Admin | Users and roles, settings for the rules, shifts, holidays (upload the yearly company calendar workbook: Office rests Sunday, Factory rests Friday), OT budget, audit log. |

## Run the demo on a laptop

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
export DATABASE_URL=sqlite:///./data/demo.db DEMO_MODE=true AI_PROVIDER=mock
python -m app.cli migrate && python -m app.cli seed-demo
uvicorn app.main:app --reload
```

Open http://localhost:8000. Demo logins (password `Demo-2026-hr`): `hr.manager`,
`hr.officer`, `hr.officer2`, `gm`, `prod.head`, `sup.dry`, `sup.pcl`, `it.admin`.
All people in the demo are SAMPLE data (IDs YTR801-816, YTOP951-990).

## Run in production

See [docs/deploy-guide.md](docs/deploy-guide.md). In short:

```bash
cp .env.example .env   # fill it in
docker compose up -d
docker compose exec app python -m app.cli create-admin it.admin
```

## Tests

```bash
pytest            # SQLite
DATABASE_URL=postgresql+psycopg://user@host/hrhub_test pytest   # PostgreSQL
```

## Documents

- [docs/deploy-guide.md](docs/deploy-guide.md): for IT, how to install, back up and update
- [docs/architecture.md](docs/architecture.md): how the parts fit together
- [docs/security-pdpa.md](docs/security-pdpa.md): access control, logging, PDPA
- [docs/ai.md](docs/ai.md): AI providers, costs, what AI does and does not decide
