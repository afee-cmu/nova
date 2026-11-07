"""FastAPI backend for the BonApp scheduling UI."""

import os
import uuid
from copy import deepcopy
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .adapters import explain
from .llm import parse_message
from .mock_data import EMPLOYEES, SCHEDULE

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SHIFT_TIMES = {"lunch": ("11:00", "16:00"), "dinner": ("17:00", "22:00")}

app = FastAPI(title="BonApp UI")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_ORIGINAL_SCHEDULE = deepcopy(SCHEDULE)
_ORIGINAL_EXPLANATION = deepcopy(explain(_ORIGINAL_SCHEDULE))
_ORIGINAL_EMPLOYEES = deepcopy(EMPLOYEES)
_DEMAND = {}
_GAP_IDS = {}
for assignment in _ORIGINAL_SCHEDULE["assignments"]:
    key = (assignment["day"], assignment["shift"], assignment["role"])
    _DEMAND[key] = _DEMAND.get(key, 0) + 1
for gap in _ORIGINAL_SCHEDULE["gaps"]:
    key = (gap["day"], gap["shift"], gap["role"])
    _DEMAND[key] = max(_DEMAND.get(key, 0), gap["needed"])
    _GAP_IDS[key] = gap["gap_id"]

current_schedule = deepcopy(_ORIGINAL_SCHEDULE)
current_explanation = deepcopy(_ORIGINAL_EXPLANATION)
current_employees = deepcopy(_ORIGINAL_EMPLOYEES)
pending_changes = {}


class UpdateRequest(BaseModel):
    message: str


class ApprovalRequest(BaseModel):
    change_id: str
    approve: bool


def _employee_by_id(employee_id):
    return next(person for person in current_employees if person["employee_id"] == employee_id)


def _recompute():
    global current_explanation
    assigned_counts = {}
    for item in current_schedule["assignments"]:
        key = (item["day"], item["shift"], item["role"])
        assigned_counts[key] = assigned_counts.get(key, 0) + 1
    gaps = []
    for key, needed in _DEMAND.items():
        assigned = assigned_counts.get(key, 0)
        if assigned < needed:
            gap_id = _GAP_IDS.setdefault(key, f"g-{key[0].lower()}-{key[1]}-{key[2]}")
            gaps.append({"gap_id": gap_id, "day": key[0], "shift": key[1], "role": key[2],
                         "needed": needed, "assigned": assigned, "short_by": needed - assigned})
    current_schedule["gaps"] = gaps
    wages = {person["employee_id"]: person["hourly_wage"] for person in current_employees}
    current_schedule["total_cost"] = round(sum(item["hours"] * wages[item["employee_id"]]
                                                 for item in current_schedule["assignments"]), 2)
    needed_slots = sum(_DEMAND.values())
    current_schedule["coverage_rate"] = (len(current_schedule["assignments"]) / needed_slots) if needed_slots else 1.0
    current_explanation = explain(current_schedule)


def _clock_minutes(value):
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _turnaround_conflicts(employee_id, candidate):
    entries = [item for item in current_schedule["assignments"] if item["employee_id"] == employee_id]
    entries.append(candidate)
    occurrences = []
    for item in entries:
        day_index = DAYS.index(item["day"])
        occurrences.append((day_index * 1440 + _clock_minutes(item["start"]),
                            day_index * 1440 + _clock_minutes(item["end"]), item))
    occurrences.sort(key=lambda value: value[0])
    conflicts = []
    for index, (_, end, item) in enumerate(occurrences):
        next_start, _, next_item = occurrences[(index + 1) % len(occurrences)]
        if index == len(occurrences) - 1:
            next_start += 7 * 1440
        rest = next_start - end
        if item["shift"] == "dinner" and next_item["shift"] == "lunch" and rest < 11 * 60:
            conflicts.append((item, next_item, rest))
    return conflicts


def _find_gap(day, shift, role):
    return next((gap for gap in current_schedule["gaps"]
                 if gap["day"] == day and gap["shift"] == shift and gap["role"] == role), None)


def _cover_assignment(employee, day, shift, role):
    start, end = SHIFT_TIMES[shift]
    return {"employee_id": employee["employee_id"], "name": employee["name"], "day": day,
            "shift": shift, "role": role, "start": start, "end": end,
            "hours": (_clock_minutes(end) - _clock_minutes(start)) / 60}


def _apply_cover(employee, day, shift, role):
    current_schedule["assignments"].append(_cover_assignment(employee, day, shift, role))
    _recompute()


def _response(reply, change=None, approval=False, reason=None, source=None, parsed=None):
    result = {"reply": reply, "change": change, "needs_manager_approval": approval,
              "reason": reason, "schedule": deepcopy(current_schedule),
              "explanation": deepcopy(current_explanation)}
    if source is not None:
        result["source"] = source
    if parsed is not None:
        result["parsed"] = deepcopy(parsed)
    return result


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    ai_available = bool(os.getenv("api_key"))
    return {"status": "ok", "mock_mode": not ai_available, "ai_available": ai_available}


@app.get("/api/schedule")
def schedule():
    return {"schedule": deepcopy(current_schedule), "explanation": deepcopy(current_explanation),
            "employees": deepcopy(current_employees)}


@app.post("/api/reset")
def reset():
    global current_schedule, current_explanation, current_employees
    current_schedule = deepcopy(_ORIGINAL_SCHEDULE)
    current_explanation = deepcopy(_ORIGINAL_EXPLANATION)
    current_employees = deepcopy(_ORIGINAL_EMPLOYEES)
    pending_changes.clear()
    return {"schedule": deepcopy(current_schedule), "explanation": deepcopy(current_explanation),
            "employees": deepcopy(current_employees)}


@app.post("/api/update")
def update(request: UpdateRequest):
    parsed, source = parse_message(request.message, current_employees, current_schedule)
    employee_name = parsed["employee"]
    employee = next((person for person in current_employees if person["name"] == employee_name), None)
    day, shift, role = parsed["day"], parsed["shift"], parsed["role"]
    action = {"type": parsed["type"], "employee": employee_name, "day": day, "shift": shift, "role": role}

    def respond(reply, change=None, approval=False, reason=None):
        return _response(reply, change, approval, reason, source=source, parsed=action)

    if parsed["type"] == "question":
        return respond(parsed["reply"])
    if parsed["type"] == "unknown":
        reply = parsed["reply"] or 'I can help with callouts and shift coverage. Try “Maria called out Friday dinner” or “Leo will cover Saturday dinner grill”.'
        return respond(reply)
    if employee is None:
        return respond(parsed["reply"] or "I couldn't match an employee name in that update. Please include a roster name and the shift details.")
    if day is None:
        return respond(parsed["reply"] or f"Which day should I update for {employee['name']}?")

    if parsed["type"] == "callout":
        assigned = [item for item in current_schedule["assignments"]
                    if item["employee_id"] == employee["employee_id"] and item["day"] == day
                    and (shift is None or item["shift"] == shift) and (role is None or item["role"] == role)]
        available_shifts = sorted({item["shift"] for item in assigned})
        if shift is None and len(available_shifts) > 1:
            shifts = ", ".join(available_shifts)
            return respond(parsed["reply"] or f"{employee['name']} has multiple shifts on {day}: {shifts}. Which shift are they calling out from?")
        if not assigned:
            return respond(parsed["reply"] or f"I couldn't find a matching shift for {employee['name']} on {day}.")
        removed = []
        for item in assigned:
            current_schedule["assignments"].remove(item)
            removed.append(item)
        _recompute()
        shift_label = ", ".join(item["shift"] for item in removed)
        return respond(parsed["reply"] or f"Updated: {employee['name']} is marked out for {day} {shift_label}. Coverage gaps have been refreshed.",
                       {"type": "callout", "employee": employee["name"], "day": day, "shifts": shift_label})

    if shift is None or role is None:
        missing = " and ".join(label for label, value in (("shift", shift), ("role", role)) if value is None)
        return respond(parsed["reply"] or f"What {missing} should {employee['name']} cover on {day}?")
    if role not in employee["roles"]:
        return respond(parsed["reply"] or f"{employee['name']} isn't listed as trained for {role}. No schedule change was made.")
    if any(item["employee_id"] == employee["employee_id"] and item["day"] == day and item["shift"] == shift
           for item in current_schedule["assignments"]):
        return respond(parsed["reply"] or f"{employee['name']} is already working {day} {shift}. No schedule change was made.")
    gap = _find_gap(day, shift, role)
    if not gap:
        return respond(parsed["reply"] or f"There's no open {role} gap on {day} {shift} to cover. No schedule change was made.")

    candidate = _cover_assignment(employee, day, shift, role)
    weekly_hours = sum(item["hours"] for item in current_schedule["assignments"]
                       if item["employee_id"] == employee["employee_id"]) + candidate["hours"]
    reasons = []
    if weekly_hours > employee["max_weekly_hours"]:
        reasons.append(f"{employee['name']} would reach {weekly_hours:g}h this week, above their {employee['max_weekly_hours']}h max.")
    if weekly_hours > 40:
        reasons.append(f"{employee['name']} would reach {weekly_hours:g}h this week, above 40h.")
    turnaround_conflicts = _turnaround_conflicts(employee["employee_id"], candidate)
    if turnaround_conflicts:
        reasons.append(f"This cover would leave less than 11 hours between a closing shift and the next opening shift for {employee['name']}.")
    if reasons:
        reason = " ".join(reasons) + " May trigger premium pay."
        change_id = str(uuid.uuid4())
        change = {"id": change_id, "type": "cover", "employee": employee["name"], "employee_id": employee["employee_id"],
                  "day": day, "shift": shift, "role": role, "hours": candidate["hours"]}
        pending_changes[change_id] = deepcopy(change)
        return respond(f"I’ve prepared {employee['name']} to cover {day} {shift} {role}, pending your approval.",
                       change, True, reason)

    _apply_cover(employee, day, shift, role)
    return respond(parsed["reply"] or f"Updated: {employee['name']} is covering {day} {shift} {role}.",
                   {"type": "cover", "employee": employee["name"], "day": day, "shift": shift, "role": role})

@app.post("/api/approve")
def approve(request: ApprovalRequest):
    change = pending_changes.pop(request.change_id, None)
    if change is None:
        return _response("That approval request is no longer available. The schedule was not changed.")
    if not request.approve:
        return _response(f"Rejected. {change['employee']}'s {change['day']} {change['shift']} cover was not applied.")
    employee = next(person for person in current_employees if person["employee_id"] == change["employee_id"])
    _apply_cover(employee, change["day"], change["shift"], change["role"])
    return _response(f"Approved: {employee['name']} is covering {change['day']} {change['shift']} {change['role']}.",
                     {"type": "cover", "employee": employee["name"], "day": change["day"],
                      "shift": change["shift"], "role": change["role"]})
