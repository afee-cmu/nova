"""Replace these mock adapters with the team's real scheduling functions."""

import re

from .mock_data import EMPLOYEES, EXPLANATION, SCHEDULE

DAYS = {
    "mon": "Mon", "monday": "Mon", "tue": "Tue", "tues": "Tue", "tuesday": "Tue",
    "wed": "Wed", "wednesday": "Wed", "thu": "Thu", "thur": "Thu", "thurs": "Thu", "thursday": "Thu",
    "fri": "Fri", "friday": "Fri", "sat": "Sat", "saturday": "Sat", "sun": "Sun", "sunday": "Sun",
}
ROLES = ("grill", "fry", "prep", "cashier", "pack")


def standardize(*args, **kwargs):
    """Mock the conversion of roster and demand inputs into structured data."""
    return {"employees": EMPLOYEES, "availability": [], "rush_windows": []}


def build_schedule(*args, **kwargs):
    """Mock the OR-Tools solver output using the agreed schedule contract."""
    return SCHEDULE


def explain(schedule=None, *args, **kwargs):
    """Mock a refreshed explanation using one plain template per current gap."""
    schedule = schedule or SCHEDULE
    gaps = schedule.get("gaps", [])
    if not gaps:
        return {"summary": "The schedule is fully covered with no open staffing gaps.", "gap_explanations": []}
    summary = f"The schedule has {len(gaps)} open coverage gap{'s' if len(gaps) != 1 else ''}. Review the suggested actions below."
    descriptions = []
    for gap in gaps:
        descriptions.append({
            "gap_id": gap["gap_id"],
            "explanation": f"{gap['day']} {gap['shift']} is short {gap['short_by']} {gap['role']} employee{'s' if gap['short_by'] != 1 else ''}.",
            "suggested_action": f"Ask an available {gap['role']}-trained employee to cover this shift.",
            "needs_manager_approval": False,
        })
    return {"summary": summary, "gap_explanations": descriptions}


def parse_update(message, employees):
    """Offline keyword fallback for schedule updates when AI parsing is unavailable."""
    normalized = (message or "").lower().replace("’", "'")
    employee = next((person for person in sorted(employees, key=lambda item: len(item["name"]), reverse=True)
                     if re.search(rf"\b{re.escape(person['name'].lower())}\b", normalized)), None)
    day = None
    for alias, label in DAYS.items():
        if re.search(rf"\b{alias}\b", normalized):
            day = label
            break
    shift = next((name for name in ("lunch", "dinner") if re.search(rf"\b{name}\b", normalized)), None)
    role = next((name for name in ROLES if re.search(rf"\b{name}\b", normalized)), None)

    callout = any(term in normalized for term in ("called out", "sick", "can't come", "cannot come", "cant come")) or bool(re.search(r"\boff\b", normalized))
    cover = any(term in normalized for term in ("cover", "covers", "will take", "can take", "pick up", "pick-up"))
    update_type = "callout" if callout else "cover" if cover else "unknown"
    return {
        "type": update_type,
        "employee": employee,
        "day": day,
        "shift": shift,
        "role": role,
    }
