"""What the AI is asked to read from each paper form, and the JSON it must return.
Every field comes back with a confidence from 0 to 1 so unclear handwriting goes
to HR instead of straight into the data."""
from __future__ import annotations


def _field(kind: str = "string") -> dict:
    return {
        "type": "object",
        "properties": {
            "value": {"type": [kind, "null"]},
            "confidence": {"type": "number"},
        },
        "required": ["value", "confidence"],
        "additionalProperties": False,
    }


OT_FORM_SCHEMA = {
    "type": "object",
    "properties": {
        "form_code": _field(),
        "work_date": _field(),
        "department": _field(),
        "shift": _field(),
        "requested_by": _field(),
        "approved_by_signed": _field("boolean"),
        "rows": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "employee_id": _field(),
                    "name": _field(),
                    "ot_from": _field(),
                    "ot_to": _field(),
                    "hours": _field("number"),
                    "job": _field(),
                    "signed": _field("boolean"),
                    "note": _field(),
                },
                "required": ["employee_id", "name", "ot_from", "ot_to", "hours", "job", "signed", "note"],
                "additionalProperties": False,
            },
        },
        "page_quality": {"type": "string", "enum": ["good", "blurry", "cut_off", "not_this_form"]},
    },
    "required": ["form_code", "work_date", "department", "shift", "requested_by", "approved_by_signed", "rows", "page_quality"],
    "additionalProperties": False,
}

OT_FORM_PROMPT = """You are reading a photo of a paper overtime form from a Thai rubber factory
(form code FM-HRM-25, "OT request / time sheet"). Text may be Thai, English or Burmese, typed or handwritten.

Read every employee row that has any writing. For each field return the value exactly as written,
normalised as follows, plus your confidence from 0 to 1 that the value is correct:
- employee_id: as written, for example YTOP898 or YTR009. If only a number is written, return the number.
- name: as written (Thai or English).
- ot_from / ot_to: 24-hour HH:MM. "18.00" means 18:00. Leave null if not written.
- hours: the OT hours written on the form as a number (1.5 for 1 hour 30 minutes). Null if blank.
- job: the job or task written for the row, if any.
- signed: true if the employee's signature box has a signature.
- work_date: YYYY-MM-DD. Thai Buddhist years (2569) must be converted to Gregorian (2026).
- approved_by_signed: true if the supervisor or department head approval box is signed.
Never guess a value you cannot read: return null with a low confidence instead.
Set page_quality to blurry, cut_off or not_this_form when that is the case."""


WORK_PERMIT_SCHEMA = {
    "type": "object",
    "properties": {
        "full_name": _field(),
        "nationality": _field(),
        "passport_no": _field(),
        "work_permit_no": _field(),
        "issue_date": _field(),
        "expiry_date": _field(),
        "employer": _field(),
        "page_quality": {"type": "string", "enum": ["good", "blurry", "cut_off", "not_this_form"]},
    },
    "required": ["full_name", "nationality", "passport_no", "work_permit_no", "issue_date", "expiry_date", "employer", "page_quality"],
    "additionalProperties": False,
}

WORK_PERMIT_PROMPT = """You are reading a photo of a Thai work permit (ใบอนุญาตทำงาน) or a pink card
for a foreign worker, usually from Myanmar. Return each field with your confidence from 0 to 1:
- full_name: in Latin letters as printed.
- nationality, passport_no (or identity document number), work_permit_no.
- issue_date and expiry_date as YYYY-MM-DD. Convert Thai Buddhist years (e.g. 2570) to Gregorian (2027).
- employer: the employer name as printed.
Never guess: return null with low confidence if a value is unreadable."""


FORMS = {
    "OT_FORM": (OT_FORM_SCHEMA, OT_FORM_PROMPT),
    "WORK_PERMIT": (WORK_PERMIT_SCHEMA, WORK_PERMIT_PROMPT),
}
