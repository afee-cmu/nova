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


def parse_update(message, employees, schedule):
    """Keyword fallback matching the current roster, tasks, and named windows."""
    normalized = (message or "").lower().replace("’", "'")
    employee = next((person for person in sorted(employees, key=lambda item: len(item["name"]), reverse=True)
                     if re.search(rf"\b{re.escape(person['name'].lower())}\b", normalized)), None)

    days_present = {window["day"] for window in schedule.get("windows", [])}
    day_aliases = {
        "Mon": ("mon", "monday"), "Tue": ("tue", "tues", "tuesday"),
        "Wed": ("wed", "wednesday"), "Thu": ("thu", "thur", "thurs", "thursday"),
        "Fri": ("fri", "friday"), "Sat": ("sat", "saturday"), "Sun": ("sun", "sunday"),
    }
    day = next((label for label in day_aliases if label in days_present and
                any(re.search(rf"\b{re.escape(alias)}\b", normalized) for alias in day_aliases[label])), None)

    windows = schedule.get("windows", [])
    day_windows = [window for window in windows if day is None or window["day"] == day]
    matched = [window for window in day_windows
               if window["name"].lower() in normalized or window["key"].lower().replace("_", " ") in normalized]
    if not matched:
        synonyms = {
            "lunch": ("lunch", "morning", "midday"),
            "dinner": ("dinner", "evening", "night", "tonight"),
        }
        for aliases in synonyms.values():
            if any(re.search(rf"\b{re.escape(alias)}\b", normalized) for alias in aliases):
                matched = [window for window in day_windows
                           if any(alias in window["name"].lower() for alias in aliases)]
                if matched:
                    break
    window_name = matched[0]["name"] if matched else None

    tasks = schedule.get("tasks", [])
    matched_tasks = [task for task in tasks if task.lower() in normalized]
    if not matched_tasks:
        task_synonyms = {
            "register": ("cashier", "register", "front counter"),
            "expo": ("expediter", "expediting", "packing"),
            "pack": ("packer", "packing", "pack"),
            "grill": ("grill", "line cook", "cooking"),
            "fry": ("fry", "fryer", "fry station"),
        }
        for task in tasks:
            aliases = [task.lower()]
            for key, synonyms in task_synonyms.items():
                if key in task.lower():
                    aliases.extend(synonyms)
            if any(re.search(rf"\b{re.escape(alias)}\b", normalized) for alias in aliases):
                matched_tasks.append(task)
    task = max(matched_tasks, key=len) if matched_tasks else None

    callout = any(term in normalized for term in ("called out", "sick", "can't come", "cannot come", "cant come")) or bool(re.search(r"\boff\b", normalized))
    cover = any(term in normalized for term in ("cover", "covers", "will take", "can take", "pick up", "pick-up"))
    update_type = "callout" if callout else "cover" if cover else "unknown"
    return {"type": update_type, "employee": employee, "day": day,
            "shift": window_name, "role": task}
