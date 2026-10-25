# AI in the HR Hub

## Principle

AI reads and suggests; people decide. Nothing AI reads becomes an OT request
or changes an employee record until an HR person accepts it, and every
accepted value goes through the same rule checks as typed data.

## What is built (Phase 1)

**Paper OT form FM-HRM-25.** HR (or a supervisor's phone) photographs the
form. AI returns every cell with a confidence score. The app then:

1. Matches each Employee ID to the master and suggests the nearest ID when it
   is unreadable (for example `YTOP95?`).
2. Compares the name on the form with the master.
3. Checks that the hours written match the times, and how much is payable OT
   after shift hours.
4. Runs the same legal checks as the web form (36h week, under 18, holiday,
   overlap, after exit).
5. Highlights in yellow every cell below the confidence threshold
   (`AI_REVIEW_THRESHOLD`, default 0.85).

HR fixes the yellow cells, ticks the clean lines and creates OT requests. If
the Department Head already signed the paper, that signature can be recorded
as the first approval step.

**Work permit.** A photo of the permit fills the permit number and expiry on
the restricted record, after HR confirms.

## Providers

Set in `.env`. The photo goes only to the provider you choose.

| `AI_PROVIDER` | Notes |
|---|---|
| `none` | AI off. Paper forms are typed in by hand. |
| `anthropic` | Claude. `AI_MODEL` defaults to `claude-opus-5-5`. |
| `openai` | OpenAI, or any OpenAI-compatible server (set `AI_BASE_URL`), including a model run in-house. |
| `gemini` | Google Gemini, if the company prefers to stay inside Google. |
| `mock` | Demo only, returns a fixed SAMPLE answer. |

Rough cost: one photo is a few cents with a hosted model. 300 forms a month
is a small monthly amount; check the provider's current price list.

## Good next uses (proposed)

| Use | Saves | Where |
|---|---|---|
| Leave forms and medical certificates read from photos | Typing, checking dates | Phase 2 leave |
| Exit interview notes coded into exit reasons | Manual coding, better turnover analysis | Phase 2 exit |
| Translate notices and payslip notes into Myanmar | Translator time | Any |
| Draft JDs from the position and skill matrix | First drafts | Phase 2 JD |
| Explain unusual OT or absence ("OT in Drying up 40% because of 3 people on night shift") | Analysis time | Dashboards |
| Answer employee questions about rules and leave balance in Thai or Myanmar | HR desk time | Later |

Each of these follows the same rule: AI proposes, a person confirms, the
app logs who confirmed.
