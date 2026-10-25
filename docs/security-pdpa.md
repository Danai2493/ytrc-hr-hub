# Security and PDPA

## Who sees what

| Role | Sees | Can do |
|---|---|---|
| Employee | Own record and own requests | View |
| Supervisor | Own team (units set in Admin > Users) | Enter OT for the team |
| Department Head | Own units and sub-units | Approve, request transfers and exits |
| HR officer | All people (restricted data hidden unless opened) | Edit records, verify OT, pay batches, review AI forms |
| HR Manager | Everything in HR including restricted data | All HR steps |
| GM | All dashboards | Final approvals, weekly-limit override |
| Auditor | Audit log and dashboards | Read only |
| Admin (IT) | Users and settings | Cannot approve HR requests |

Scope is checked on the server for every page, not only hidden in menus.

## Data kept out of the app

Salaries, bank accounts and pay rates stay in Bplus. The app sends hours by
pay code and receives totals by Org Unit only.

## Restricted data

ID card number, passport, work permit and visa sit in a separate table.
Opening them requires the HR role and writes a `view_restricted` entry to the
audit log.

## Audit log

Every sign-in (and failed try), create, change (with before and after
values), approval, export, import and restricted view is logged with user,
time and IP address. The log cannot be edited from the app.

## Sign-in and sessions

- Google Workspace sign-in limited to the company domain.
- App-only logins: scrypt hashes, at least 10 characters with letters and numbers, lockout after 5
  wrong tries, temporary passwords shown once.
- Signed session cookie (HttpOnly, SameSite=Lax, Secure over HTTPS),
  session ends after `SESSION_HOURS`.
- CSRF token on every form.
- Security headers: Content-Security-Policy, X-Frame-Options DENY,
  same-origin referrer, nosniff.

## Paper form photos

Photos are re-saved on upload to remove location and camera data (EXIF),
stored on the company server, and linked to the OT lines they created. When
AI reading is on, the photo is sent to the chosen AI provider; see `ai.md`.

## PDPA checklist for go-live

- [ ] Privacy notice to employees covers HR system processing and AI reading of forms.
- [ ] Data processing agreement with the hosting and AI provider.
- [ ] Retention: decide how long to keep form photos (suggest 2 years) and exited employees' data.
- [ ] Access review by the HR Manager every quarter (Admin > Users).
- [ ] Backups encrypted and stored off the server.
- [ ] Demo databases use SAMPLE data only.
