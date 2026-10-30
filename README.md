# YTRC HR Hub

One web app for HR work and HR monitoring: employee master, org chart and
positions, OT requests and approvals, face scan check, pay input to Bplus 7.4,
AI reading of paper forms, data health checks and dashboards. Screens in Thai,
English and Myanmar.

Phase 1 (HR Foundation) and Phase 2 (leave, exit, skills, training,
onboarding, probation) of the 5-level HR blueprint are built. Later phases
(appraisal, succession, workforce planning, predictive flags) plug into the
same data model and approval engine.

## What it does today

| Area | What people do |
|---|---|
| People | Search, view, create and correct employee records (a second HR person checks). Transfers, promotions, exits as dated events with history. Restricted data (ID card, passport, work permit) visible to HR only and every view logged. |
| Org & positions | Org chart, Department Heads, budgeted seats, vacancies, critical positions. |
| OT | Supervisor enters OT like form FM-HRM-25 with live checks (36h weekly limit, under-18 rule, holiday calendar, overlap, shift hours). Department Head approves. GM override for the weekly limit is logged. |
| Face scan | Import the timelog file. Daily attendance, late, stayed after shift. Each OT line is checked against the scans and short lines are paid only for covered time. |
| Pay input | HR verifies OT, builds a batch per pay cycle, a second HR person checks it, the CSV goes into Bplus. Totals by unit come back from Bplus for reconciliation. Salaries and bank data stay in Bplus. |
| Paper forms (AI) | Photograph a paper OT form, leave form or work permit. AI reads it, every line goes through the same checks as the web form, HR fixes the unclear cells and accepts. |
| Leave | Employees, supervisors or HR request leave with live checks (company calendar, days left under the Labour Protection Act, overlap, medical certificate, OT or scans on leave days). Department Head approves; approved leave replaces "absent" in attendance. HR can photograph a paper leave form and AI fills it in. |
| Exit | An approved exit opens a clearance checklist (handover, assets, ID card, IT accounts, final pay to Bplus, social security notice by the 15th, work permit notice). Exit interview; AI suggests the reason code, HR confirms. |
| Skills & training | ILUO skills matrix per unit: what each job needs, each person's level, and how many people are at U/O for every critical skill. Gaps, lapsed licences and induction become training needs; recording a class closes them and raises the level. Job descriptions per job with an AI first draft that HR edits. |
| Recruitment | A Department Head asks for people against a budgeted position; the Department Head and HR Manager approve, and the GM too for new headcount or anything outside the budget. AI drafts the job ad from the JD (never shows pay). Candidates come in by form or by a photo/PDF of their CV or paper application that AI reads for HR to check. Screening checks run on their own: under 15 / juvenile, work permit, former employee (blocks rehire after termination), duplicates, active employees, PDPA consent. Interview scorecards on the job's key skills and Growth Compass competencies, with AI questions fitted to the candidate. Offer, then Hire sends the new-employee request (second HR check) and opens onboarding and the probation review. Dashboard: open seats, days to fill, funnel, sources, why candidates were not selected or withdrew. Candidate data is erased after the retention period. |
| Growth (competencies) | YTRC Growth Compass, our own framework: 20 competencies in 5 groups, 60 behaviour questions in Thai and English (short set for operators, more for staff and team leaders). HR opens a round once or twice a year; people answer on the web or on paper (HR types it in). Each person gets a radar profile with strengths, areas to develop, change since last round, an optional supervisor view and growth paths. No overall score or ranking, not used for pay. A development plan (70/20/10) with AI action ideas; agreed course actions become training needs. Team averages only for groups of 5 or more. |
| New employees | An approved hire opens an onboarding checklist (contract, social security within 30 days, Bplus, face scan, PPE, induction, buddy). A probation review opens before day 119 with attendance and skills filled in; a fail needs reasons and the HR Manager, and opens the exit case. |
| Data health | 25 rules (ID format, missing fields, juvenile workers, permits expiring, OT without request, unknown scan IDs, open exit clearance, social security deadline, sick leave without certificate, skills short of U/O people, lapsed licences, late onboarding, undecided probation...). Issues have owners and close themselves when fixed. |
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
