"""Notes path: a manager's plain-English notes -> reviewed roster -> the same schedule the workbook path makes.

Reuses pipeline.py (LLM extraction, validation, plain-English corrections) and converts the
result into the exact `inputs` shape solver_bridge.normalize()/build_explanation()/propose_replan()
already understand, so the grid, Insights, call-out re-plans and approvals all work unchanged.
"""

import os
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_SETUP = ROOT / "sample_restaurant_setup.txt"
SAMPLE_AVAILABILITY = ROOT / "sample_availability.txt"
MAX_NOTES_CHARS = 20_000
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class NotesError(ValueError):
    """A problem the manager can fix (missing key, empty notes, unreadable notes)."""


def _require_key():
    if not os.getenv("api_key"):
        raise NotesError("Reading notes uses AI. Set the api_key environment variable and restart the server.")


def samples():
    read = lambda p: p.read_text() if p.exists() else ""
    return {"setup_text": read(SAMPLE_SETUP), "availability_text": read(SAMPLE_AVAILABILITY)}


def extract(setup_text, availability_text):
    """Run the two LLM extraction steps. Returns a draft dict (kept server-side)."""
    _require_key()
    setup_text, availability_text = (setup_text or "").strip(), (availability_text or "").strip()
    if not setup_text or not availability_text:
        raise NotesError("Paste both your weekly setup notes and your staff availability notes.")
    if len(setup_text) > MAX_NOTES_CHARS or len(availability_text) > MAX_NOTES_CHARS:
        raise NotesError(f"Notes must be under {MAX_NOTES_CHARS:,} characters each.")

    import pipeline
    from scheduler import build_shifts

    try:
        config = pipeline.ask_for_json(pipeline.SETUP_PROMPT, setup_text, pipeline.check_setup)
        shifts, demand = build_shifts(config["rush_profile"])
        prompt = (pipeline.AVAILABILITY_PROMPT
                  .replace("{ROLES}", ", ".join(config["default_role_rates"]))
                  .replace("{SHIFTS}", ", ".join(shifts)))
        roster = pipeline.ask_for_json(prompt, availability_text,
                                       pipeline.make_employee_check(config, shifts, demand))
    except RuntimeError as error:
        raise NotesError(f"The notes couldn't be read reliably: {error}") from error
    return {"config": config, "employees": roster["employees"],
            "assumptions": roster.get("assumptions", []), "history": []}


def correct(draft, message):
    """Apply one plain-English correction. Returns the code-generated list of changes."""
    _require_key()
    message = (message or "").strip()
    if not message:
        raise NotesError("Type a correction, e.g. \"Chris can't do Saturday lunch\".")

    import pipeline
    from scheduler import build_shifts

    config = draft["config"]
    shifts, demand = build_shifts(config["rush_profile"])
    prompt = (pipeline.CORRECTION_PROMPT
              .replace("{ROLES}", ", ".join(config["default_role_rates"]))
              .replace("{SHIFTS}", ", ".join(shifts)))
    current = {"employees": draft["employees"], "assumptions": draft["assumptions"]}
    try:
        import json
        updated = pipeline.ask_for_json(prompt, f"Current JSON:\n{json.dumps(current)}\n\nManager's correction: {message}",
                                        pipeline.make_employee_check(config, shifts, demand))
    except RuntimeError as error:
        raise NotesError(f"Couldn't apply that: {error}") from error

    changes = pipeline.describe_changes(draft["employees"], updated["employees"])
    if changes:
        draft["history"].append((deepcopy(draft["employees"]), deepcopy(draft["assumptions"])))
        draft["employees"] = updated["employees"]
        draft["assumptions"] = updated.get("assumptions", draft["assumptions"])
    return changes


def undo(draft):
    if not draft["history"]:
        return False
    draft["employees"], draft["assumptions"] = draft["history"].pop()
    return True


def to_inputs(draft):
    """Build the same `inputs` dict solver_bridge.load_workbook_input() returns."""
    from scheduler import build_shifts

    config = deepcopy(draft["config"])
    employees = deepcopy(draft["employees"])
    shifts, demand = build_shifts(config["rush_profile"])
    raw_people = [{
        "employee_id": e["id"], "name": e["name"],
        "task_skills": {role: {"hourly_rate": wage} for role, wage in e["roles"].items()},
        "min_hours_week": e.get("min_hours"), "max_hours_week": e.get("max_hours"),
    } for e in employees]
    return {
        "raw_data": {"tasks": {role: {} for role in config["default_role_rates"]}, "employees": raw_people},
        "scheduler_employees": employees,
        "config": config,
        "shifts": shifts,
        "demand": demand,
        "warnings": [],
    }


def view(draft):
    """What the manager sees on the review screen ("Here's what I understood")."""
    from scheduler import build_shifts

    config = draft["config"]
    shifts, demand = build_shifts(config["rush_profile"])
    windows = []
    for name, info in shifts.items():
        staff = {d["role"]: d["needed"] for d in demand if d["shift"] == name}
        windows.append({"key": name, "day": info["day"], "name": info["name"],
                        "start": info["start"], "end": info["end"], "staff": staff})
    people = []
    for e in draft["employees"]:
        by_day = {d: [] for d in DAYS}
        for shift in e["available"]:
            day, window = shift.split("_", 1)
            by_day.setdefault(day, []).append(window)
        people.append({"id": e["id"], "name": e["name"],
                       "roles": [{"role": r, "wage": w if w is not None else config["default_role_rates"].get(r),
                                  "default_pay": w is None} for r, w in e["roles"].items()],
                       "min_hours": e.get("min_hours"), "max_hours": e.get("max_hours"),
                       "max_shifts": e.get("max_shifts"),
                       "availability": [{"day": d, "windows": by_day[d]} for d in DAYS if by_day.get(d)]})
    return {"roles": config["default_role_rates"], "windows": windows, "employees": people,
            "assumptions": config.get("assumptions", []) + draft["assumptions"],
            "can_undo": bool(draft["history"])}
