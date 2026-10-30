"""Screen text in English, Thai and Myanmar. The English text is the key, so a
missing translation simply shows English. Translations live in app/i18n/*.json
and can be corrected by HR without touching code."""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

LANGS = {"th": "ไทย", "en": "English", "my": "မြန်မာ"}
DIR = Path(__file__).parent / "i18n"

# Messages for rule findings (timecalc.Finding codes and capture issue codes).
RULE_TEXT = {
    "req_no_position": "No position chosen: the request counts as outside the budget",
    "req_frozen": "Position {pos} is frozen",
    "req_over_budget": "Asks for {n} but only {free} budgeted seat(s) are free: the GM approves",
    "req_replace_who": "Choose the employee who is being replaced",
    "req_replace_active": "{emp} is still active with no approved exit",
    "req_past": "The needed-by date is in the past",
    "req_short_notice": "Needed in {days} days: hiring usually takes 3 to 6 weeks",
    "req_no_jd": "Job {job} has no job description yet: write it in Skills > Jobs so the ad and interview fit",
    "cand_id_check": "ID card number fails the check digit: please check it",
    "cand_duplicate": "Same phone or ID card as {ref} {name}",
    "cand_active_employee": "This person is an active employee ({emp})",
    "cand_rehire_blocked": "Former employee {emp} left on {left} ({reason}): not eligible for rehire",
    "cand_rehire_check": "Former employee {emp} left on {left} ({reason}): HR Manager decides",
    "cand_former": "Former employee {emp}, left on {left} ({reason})",
    "cand_no_consent": "No PDPA consent recorded yet",
    "app_req_closed": "Request {ref} is {status}",
    "cand_no_birth": "No birth date: age cannot be checked",
    "cand_under_15": "Age {age}: Thai law does not allow work under 15",
    "cand_juvenile": "Age {age}: juvenile worker, no OT, holiday or night work; tell the labour office within 15 days of hiring",
    "cand_permit": "Foreign worker: needs a valid work permit for this employer before the first day",
    "app_other_open": "Also in {refs}",
    "emp_unknown": "Employee not found",
    "time_order": "OT end must be after OT start",
    "before_join": "Date is before the join date ({join})",
    "after_exit": "Date is after the exit date ({exit})",
    "inactive": "Employee is not active",
    "juvenile_ot": "Worker is under 18 (age {age}): OT and holiday work are not allowed",
    "juvenile_night": "Worker is under 18: no work between 22:00 and 06:00",
    "no_birth_date": "No birth date in the master, so the under-18 check cannot run",
    "inside_shift": "{minutes} minutes fall inside normal shift hours and are not OT",
    "weekly_limit": "Week total would be {hours}h, over the {limit}h legal limit",
    "daily_limit": "{hours}h is over the daily limit of {limit}h",
    "very_long": "{hours}h of OT in one day: please check",
    "overlap": "Overlaps another OT request ({ref})",
    "after_the_fact": "Entered after the work date (not pre-approved)",
    "far_future": "Date is more than a month ahead",
    "no_payable_minutes": "No OT minutes outside the shift",
    "no_scan": "No face scan for this date yet",
    "scan_short": "Scans cover {covered}h of {requested}h (in {first_in}, out {last_out})",
    "low_confidence": "AI is not sure about: {fields}",
    "id_not_found": "ID {id} not found in the master {guess}",
    "name_mismatch": "Name on form ({form}) differs from master ({master})",
    "dup_on_form": "Same person twice on this form",
    "time_unreadable": "OT times unreadable",
    "hours_mismatch": "Hours written ({written}) do not match the times ({times}h)",
    "hours_not_payable": "Hours written ({written}) but only {payable}h are payable OT",
    "hours_unreadable": "Hours unreadable",
    "not_signed": "Employee did not sign",
    "dropped": "Dropped by HR: {note}",
    "leave_dates": "The end date is before the start date",
    "leave_type": "Choose a leave type",
    "leave_not_for": "{type} does not apply to this employee",
    "leave_no_workdays": "No working days in these dates (rest days or holidays only)",
    "leave_overlap": "Overlaps another leave ({ref})",
    "leave_over_balance": "{days} days asked but only {left} paid days left in {year}: the extra days are unpaid",
    "leave_service": "Needs {days} days of service first: please check with HR",
    "leave_document": "A medical certificate is needed from {days} days of sick leave",
    "leave_notice": "Should be asked at least {days} days ahead",
    "leave_after": "Entered after the leave started",
    "leave_ot_same_day": "There is OT on a leave day ({ref}, {date})",
    "leave_scanned": "Face scan shows work on a leave day ({dates})",
    "leave_probation": "Vacation can be used from {date}, after probation",
    "leave_left": "{left} days left in {year}",
    "leave_days_mismatch": "Days written ({written}) do not match the dates ({days} working days)",
}


@lru_cache
def _load(lang: str) -> dict:
    p = DIR / f"{lang}.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def gettext(lang: str, text: str, **kw) -> str:
    out = _load(lang).get(text, text)  # en.json only names internal codes
    if kw:
        try:
            return out.format(**kw)
        except (KeyError, IndexError, ValueError):
            return text.format(**kw) if "{" in text else out
    return out


def rule_text(lang: str, finding: dict) -> str:
    code = finding.get("code", "")
    base = RULE_TEXT.get(code, code)
    params = dict(finding.get("params") or {})
    if code == "id_not_found" and params.get("guess"):
        params["guess"] = gettext(lang, "(maybe {guess}?)", guess=params["guess"])
    return gettext(lang, base, **params)
