"""FastAPI backend for the BonApp scheduling UI."""

import os
import logging
import re
import tempfile
import uuid
from copy import deepcopy
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .llm import parse_message
from .mock_data import EMPLOYEES as MOCK_EMPLOYEES, SCHEDULE as MOCK_SCHEDULE
from .solver_bridge import (TEMPLATE_PATH, WorkbookValidationError, build_explanation,
                            initialize_demo, normalize_mock, propose_replan,
                            solve_workbook)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
logger = logging.getLogger(__name__)

app = FastAPI(title="BonApp UI")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class UpdateRequest(BaseModel):
    message: str


class ApprovalRequest(BaseModel):
    change_id: str
    approve: bool


current_schedule = normalize_mock()
current_explanation = build_explanation(current_schedule)
current_employees = deepcopy(current_schedule["employees"])
current_solver_context = None
pending_changes = {}
_BOOT_SCHEDULE = deepcopy(current_schedule)
_BOOT_EXPLANATION = deepcopy(current_explanation)
_BOOT_EMPLOYEES = deepcopy(current_employees)
_BOOT_SOLVER_CONTEXT = None


@app.on_event("startup")
def initialize_solver_cache():
    global current_schedule, current_explanation, current_employees, current_solver_context
    global _BOOT_SCHEDULE, _BOOT_EXPLANATION, _BOOT_EMPLOYEES, _BOOT_SOLVER_CONTEXT
    current_schedule, current_explanation, current_employees, current_solver_context, _ = initialize_demo()
    _BOOT_SCHEDULE = deepcopy(current_schedule)
    _BOOT_EXPLANATION = deepcopy(current_explanation)
    _BOOT_EMPLOYEES = deepcopy(current_employees)
    _BOOT_SOLVER_CONTEXT = deepcopy(current_solver_context)
    pending_changes.clear()


def _response(reply, change=None, approval=False, reason=None, source=None, parsed=None):
    result = {"reply": reply, "change": change, "needs_manager_approval": approval,
              "reason": reason, "schedule": deepcopy(current_schedule),
              "explanation": deepcopy(current_explanation), "employees": deepcopy(current_employees)}
    if source is not None:
        result["source"] = source
    if parsed is not None:
        result["parsed"] = deepcopy(parsed)
    return result


def _window_matches(day, window_name):
    return [window for window in current_schedule["windows"]
            if window["day"] == day and (window_name is None or window["name"].casefold() == window_name.casefold())]


def _recompute():
    global current_explanation
    assigned_counts = {}
    for assignment in current_schedule["assignments"]:
        key = (assignment["window_key"], assignment["task"])
        assigned_counts[key] = assigned_counts.get(key, 0) + 1
    demand = []
    gaps = []
    windows = {window["key"]: window for window in current_schedule["windows"]}
    for item in current_schedule["demand"]:
        key = (item["window_key"], item["task"])
        assigned = assigned_counts.get(key, 0)
        missing = max(0, item["needed"] - assigned)
        updated = {**item, "assigned": assigned, "missing": missing}
        demand.append(updated)
        if missing:
            window = windows[item["window_key"]]
            gaps.append({"gap_id": f"{item['window_key']}|{item['task']}",
                         "window_key": item["window_key"], "day": window["day"],
                         "window": window["name"], "task": item["task"],
                         "needed": item["needed"], "assigned": assigned, "short_by": missing})
    current_schedule["demand"] = demand
    current_schedule["gaps"] = gaps
    current_schedule["labor_cost"] = round(sum(item["cost"] for item in current_schedule["assignments"]), 2)
    needed = sum(item["needed"] for item in demand)
    missing = sum(item["missing"] for item in demand)
    current_schedule["coverage_rate"] = 1 - missing / needed if needed else 1.0
    hours = {}
    for item in current_schedule["assignments"]:
        hours[item["employee_id"]] = hours.get(item["employee_id"], 0) + item["hours"]
    for employee in current_employees:
        employee["hours_scheduled"] = hours.get(employee["employee_id"], 0)
    current_schedule["employees"] = deepcopy(current_employees)
    current_explanation = build_explanation(current_schedule, current_solver_context)


def _clock_minutes(value):
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _hours_for_window(window):
    return (_clock_minutes(window["end"]) - _clock_minutes(window["start"])) / 60


def _turnaround_conflicts(employee_id, candidate):
    entries = [item for item in current_schedule["assignments"] if item["employee_id"] == employee_id]
    entries.append(candidate)
    occurrences = []
    for item in entries:
        day_index = DAYS.index(item["day"])
        occurrences.append((day_index * 1440 + _clock_minutes(item["start"]),
                            day_index * 1440 + _clock_minutes(item["end"]), item))
    occurrences.sort(key=lambda value: value[0])
    for index, (_, end, item) in enumerate(occurrences):
        next_start, _, next_item = occurrences[(index + 1) % len(occurrences)]
        next_day = next_item["day"]
        if index == len(occurrences) - 1:
            next_start += 7 * 1440
        elif next_day == item["day"]:
            continue
        if next_start - end < 11 * 60:
            return item, next_item
    return None


def _overtime_threshold(employee_id):
    raw_data = (current_solver_context or {}).get("raw_data", {})
    person = next((entry for entry in raw_data.get("employees", [])
                   if entry["employee_id"] == employee_id), {})
    return person.get("overtime_threshold_hours")


def _make_cover_assignment(employee, window, task):
    wage = employee.get("rates", {}).get(task)
    if wage is None:
        return None
    hours = _hours_for_window(window)
    return {"employee_id": employee["employee_id"], "name": employee["name"],
            "window_key": window["key"], "day": window["day"], "window": window["name"],
            "start": window["start"], "end": window["end"], "task": task,
            "hours": hours, "wage": wage, "cost": round(hours * wage, 2)}


def _apply_cover(employee, window, task):
    assignment = _make_cover_assignment(employee, window, task)
    if assignment is None:
        raise ValueError(f"No pay rate is available for {task}.")
    current_schedule["assignments"].append(assignment)
    _recompute()


def _find_gap(window_key, task):
    return next((gap for gap in current_schedule["gaps"]
                 if gap["window_key"] == window_key and gap["task"].casefold() == task.casefold()), None)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/template")
def download_template():
    return FileResponse(TEMPLATE_PATH, filename="schedule_input_template.xlsx",
                        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.get("/api/health")
def health():
    ai_available = bool(os.getenv("api_key"))
    return {"status": "ok", "mock_mode": current_schedule["data_source"] == "mock",
            "ai_available": ai_available}


@app.get("/api/schedule")
def schedule():
    return {"schedule": deepcopy(current_schedule), "explanation": deepcopy(current_explanation),
            "employees": deepcopy(current_employees)}


@app.post("/api/upload")
async def upload_workbook(request: Request):
    """Validate and solve a canonical .xlsx workbook without touching the repo."""
    global current_schedule, current_explanation, current_employees, current_solver_context
    temporary_path = None
    form = None
    try:
        try:
            form = await request.form()
        except AssertionError as error:
            if "python-multipart" in str(error):
                return JSONResponse(status_code=500, content={"ok": False,
                    "error": "Workbook uploads require python-multipart. Install the UI dependencies and restart the server."})
            raise
        upload_values = [value for _, value in form.multi_items()
                         if hasattr(value, "filename") and hasattr(value, "read")]
        if len(upload_values) > 1:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Upload one .xlsx workbook at a time."})
        uploaded = form.get("file")
        if uploaded is None or not hasattr(uploaded, "filename") or not hasattr(uploaded, "read"):
            return JSONResponse(status_code=400, content={"ok": False, "error": "Choose one .xlsx workbook to upload."})
        filename = uploaded.filename or ""
        if Path(filename).suffix.casefold() != ".xlsx":
            return JSONResponse(status_code=400, content={"ok": False, "error": "Only .xlsx workbooks are accepted."})
        content = await uploaded.read(MAX_UPLOAD_BYTES + 1)
        if len(content) > MAX_UPLOAD_BYTES:
            return JSONResponse(status_code=413, content={"ok": False, "error": "The workbook must be 5 MB or smaller."})
        with tempfile.NamedTemporaryFile(prefix="bonapp-upload-", suffix=".xlsx", delete=False) as temp_file:
            temporary_path = Path(temp_file.name)
            temp_file.write(content)
        new_schedule, new_explanation, new_employees, inputs, solve_seconds = solve_workbook(temporary_path)
        new_schedule["data_source"] = "upload"
        current_schedule = new_schedule
        current_explanation = new_explanation
        current_employees = new_employees
        current_solver_context = inputs
        pending_changes.clear()
        return {"ok": True, "schedule": deepcopy(current_schedule),
                "explanation": deepcopy(current_explanation), "employees": deepcopy(current_employees),
                "warnings": list(current_schedule.get("warnings", [])), "solve_seconds": solve_seconds}
    except WorkbookValidationError as error:
        return JSONResponse(status_code=422, content={"ok": False, "errors": error.errors,
                            "warnings": error.warnings})
    except Exception:
        logger.exception("Unexpected workbook upload failure")
        return JSONResponse(status_code=500, content={"ok": False,
                            "error": "The workbook could not be processed. Check the file and try again."})
    finally:
        if form is not None and hasattr(form, "close"):
            await form.close()
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                logger.exception("Could not remove temporary workbook upload")


@app.post("/api/reset")
def reset():
    global current_schedule, current_explanation, current_employees, current_solver_context
    current_schedule = deepcopy(_BOOT_SCHEDULE)
    current_explanation = deepcopy(_BOOT_EXPLANATION)
    current_employees = deepcopy(_BOOT_EMPLOYEES)
    current_solver_context = deepcopy(_BOOT_SOLVER_CONTEXT)
    pending_changes.clear()
    return {"schedule": deepcopy(current_schedule), "explanation": deepcopy(current_explanation),
            "employees": deepcopy(current_employees)}


@app.post("/api/update")
def update(request: UpdateRequest):
    parsed, source = parse_message(request.message, current_employees, current_schedule)
    employee_name = parsed["employee"]
    employee = next((person for person in current_employees if person["name"] == employee_name), None)
    day, window_name, task = parsed["day"], parsed["shift"], parsed["role"]
    action = {"type": parsed["type"], "employee": employee_name, "day": day,
              "shift": window_name, "role": task}

    def respond(reply, change=None, approval=False, reason=None):
        return _response(reply, change, approval, reason, source=source, parsed=action)

    if parsed["type"] == "question":
        return respond(parsed["reply"])
    if parsed["type"] == "unknown":
        return respond(parsed["reply"] or 'I can help with callouts and window coverage. Try “a rostered employee called out Friday Dinner Rush” or “cover Friday Dinner Rush on Grill”.')
    if employee is None:
        return respond(parsed["reply"] or "I couldn't match an employee name in the roster. Please include the employee and window details.")
    if parsed["type"] != "callout" and day is None:
        return respond(parsed["reply"] or f"Which day should I update for {employee['name']}?")

    if parsed["type"] == "callout":
        original_schedule = deepcopy(current_schedule)
        original_inputs = deepcopy(current_solver_context)
        callout_window = window_name
        full_week = bool(re.search(r"\b(all week|whole week|entire week|this week|for the week|rest of the week)\b",
                                   request.message.casefold()))
        if day is None and not full_week:
            return respond(parsed["reply"] or f"Which day or window is {employee['name']} calling out from?")
        if full_week:
            window_name = None
            task = None
        if window_name is not None and day is not None and not _window_matches(day, window_name):
            return respond(parsed["reply"] or f"I couldn't find {window_name} on {day}.")
        assignments = [item for item in current_schedule["assignments"]
                       if item["employee_id"] == employee["employee_id"]
                       and (day is None or item["day"] == day)
                       and (window_name is None or item["window"].casefold() == window_name.casefold())
                       and (window_name is None or task is None or item["task"].casefold() == task.casefold())]
        if not assignments:
            scope = f"{day} {window_name}".strip() if day else "this week"
            return respond(parsed["reply"] or f"I couldn't find a scheduled assignment for {employee['name']} in {scope}.")
        for item in assignments:
            current_schedule["assignments"].remove(item)
        _recompute()
        labels = ", ".join(f"{item['day']} {item['window']} {item['task']}" for item in assignments)
        scope = labels if window_name or day is None else f"{day}"

        cancellation = {"employee_id": employee["employee_id"]}
        cancelled_window_key = None
        if window_name is not None and day is not None:
            cancelled_window_key = next(item["window_key"] for item in assignments)
            cancellation["shift"] = cancelled_window_key
        elif day is not None:
            cancellation["day"] = day

        proposal, replan_reason = propose_replan(
            {"schedule": original_schedule, "solver_inputs": original_inputs},
            employee["employee_id"], window_key=cancelled_window_key,
            day=day if cancelled_window_key is None and day is not None else None,
        )
        if proposal is None:
            reply = (f"I removed {employee['name']} from {scope}. Re-planning is not available right now, "
                     "so the shift is uncovered.")
            change = {"type": "callout", "employee": employee["name"], "day": day,
                      "window": callout_window, "reason": replan_reason}
            return respond(reply, change)

        raw_diff = proposal["diff"]
        cancelled_triples = {(item["employee_id"], item["window_key"], item["task"].casefold())
                             for item in assignments}
        extra_removed = [item for item in raw_diff.get("removed", [])
                         if (item["employee_id"], item["shift"], item["role"].casefold()) not in cancelled_triples]
        display_diff = {**raw_diff, "removed": extra_removed}
        added_count = len(raw_diff.get("added", []))
        removed_count = len(extra_removed)
        change_count = added_count + len(raw_diff.get("removed", []))
        cost_delta = proposal["schedule"]["labor_cost"] - original_schedule["labor_cost"]
        cost_change = f"+${cost_delta:,.2f}" if cost_delta >= 0 else f"-${abs(cost_delta):,.2f}"
        summary = (f"Re-plan: {change_count} change{'s' if change_count != 1 else ''} "
                   f"(+{added_count} added, -{removed_count} removed besides the call-out). "
                   f"Labor cost {cost_change}.")
        change_id = str(uuid.uuid4())
        flags = proposal["flags"]
        change = {"change_id": change_id, "kind": "replan", "diff": display_diff,
                  "flags": flags, "summary": summary}
        pending_changes[change_id] = {
            **change, "employee": employee["name"], "day": day,
            "window": callout_window, "schedule": proposal["schedule"],
            "problems": proposal["problems"], "solve_seconds": proposal["solve_seconds"],
        }
        reply = f"I removed {employee['name']} from {scope}. BonApp has a re-plan ready for your approval."
        return respond(reply, change)

    matching_windows = _window_matches(day, window_name)
    if window_name is None:
        return respond(parsed["reply"] or f"Which window should {employee['name']} cover on {day}?")
    if task is None:
        return respond(parsed["reply"] or f"Which task should {employee['name']} cover during {window_name} on {day}?")
    matching_windows = _window_matches(day, window_name)
    if not matching_windows:
        return respond(f"I couldn't find {window_name} on {day}. No schedule change was made.")
    if len(matching_windows) > 1:
        return respond(f"More than one window is named {window_name} on {day}; please specify a more exact window.")
    window = matching_windows[0]
    if task not in employee["tasks"]:
        return respond(f"{employee['name']} isn't listed as trained for {task}. No schedule change was made.")
    if any(item["employee_id"] == employee["employee_id"] and item["window_key"] == window["key"]
           for item in current_schedule["assignments"]):
        return respond(f"{employee['name']} is already working {day} {window_name}. No schedule change was made.")
    if not _find_gap(window["key"], task):
        return respond(f"There's no open {task} gap in {window_name} on {day}. No schedule change was made.")

    candidate = _make_cover_assignment(employee, window, task)
    if candidate is None:
        return respond(f"I don't have a pay rate for {employee['name']} on {task}. No schedule change was made.")
    weekly_hours = employee["hours_scheduled"] + candidate["hours"]
    reasons = []
    max_hours = employee.get("max_hours")
    if max_hours is not None and weekly_hours > max_hours:
        reasons.append(f"{employee['name']} would reach {weekly_hours:g}h this week, above their {max_hours:g}h max.")
    if weekly_hours > 40:
        reasons.append(f"{employee['name']} would reach {weekly_hours:g}h this week, above 40h.")
    threshold = _overtime_threshold(employee["employee_id"])
    if threshold is not None and weekly_hours > threshold:
        reasons.append(f"{employee['name']} would exceed their {threshold:g}h overtime threshold.")
    turnaround = _turnaround_conflicts(employee["employee_id"], candidate)
    if turnaround:
        reasons.append(f"This cover would leave less than 11 hours between {turnaround[0]['window']} and {turnaround[1]['window']} for {employee['name']}.")
    if reasons:
        reason = " ".join(reasons) + " May trigger premium pay."
        change_id = str(uuid.uuid4())
        change = {"id": change_id, "type": "cover", "employee": employee["name"],
                  "employee_id": employee["employee_id"], "day": day,
                  "window_key": window["key"], "window": window["name"],
                  "task": task, "hours": candidate["hours"]}
        pending_changes[change_id] = deepcopy(change)
        return respond(f"I’ve prepared {employee['name']} to cover {day} {window_name} {task}, pending your approval.",
                       change, True, reason)

    _apply_cover(employee, window, task)
    return respond(parsed["reply"] or f"Updated: {employee['name']} is covering {day} {window_name} {task}.",
                   {"type": "cover", "employee": employee["name"], "day": day,
                    "window": window["name"], "task": task})


@app.post("/api/approve")
def approve(request: ApprovalRequest):
    change = pending_changes.pop(request.change_id, None)
    if change is None:
        return _response("That approval request is no longer available. The schedule was not changed.")
    if change.get("kind") == "replan":
        if not request.approve:
            return _response("Kept the schedule with the uncovered shift.",
                             {"kind": "replan", "approved": False})
        global current_schedule, current_explanation, current_employees
        current_schedule = deepcopy(change["schedule"])
        current_employees = deepcopy(current_schedule["employees"])
        current_schedule["employees"] = deepcopy(current_employees)
        current_explanation = build_explanation(current_schedule, current_solver_context)
        return _response("Re-plan approved. The updated schedule is now in effect.",
                         {"kind": "replan", "approved": True, "diff": change["diff"]})
    if not request.approve:
        return _response(f"Rejected. {change['employee']}'s {change['day']} {change['window']} cover was not applied.")
    employee = next(person for person in current_employees if person["employee_id"] == change["employee_id"])
    window = next(item for item in current_schedule["windows"] if item["key"] == change["window_key"])
    _apply_cover(employee, window, change["task"])
    return _response(f"Approved: {employee['name']} is covering {change['day']} {change['window']} {change['task']}.",
                     {"type": "cover", "employee": employee["name"], "day": change["day"],
                      "window": change["window"], "task": change["task"]})
