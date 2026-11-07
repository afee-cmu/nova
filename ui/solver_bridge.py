"""Read-only adapter between teammate scheduler formats and BonApp UI V2."""

from copy import deepcopy
from pathlib import Path

from .mock_data import EMPLOYEES as MOCK_EMPLOYEES, EXPLANATION as MOCK_EXPLANATION, SCHEDULE as MOCK_SCHEDULE

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = ROOT / "schedule_input_template.xlsx"


class DemoInputError(RuntimeError):
    def __init__(self, message, warnings=None):
        super().__init__(message)
        self.warnings = warnings or []


class WorkbookValidationError(ValueError):
    def __init__(self, errors, warnings=None):
        super().__init__("Workbook validation failed")
        self.errors = list(errors)
        self.warnings = list(warnings or [])


def load_workbook_input(path):
    """Load any canonical workbook and prepare it for the scheduler."""
    from schedule_input import load_workbook_input, to_scheduler_inputs

    data, issues = load_workbook_input(path)
    if issues.has_errors():
        raise WorkbookValidationError(issues.errors, issues.warnings)
    if data is not None:
        employees, config = to_scheduler_inputs(data)
    else:
        raise WorkbookValidationError(["Workbook did not produce normalized input data."], issues.warnings)

    # Import lazily so the UI can still start with mock data if OR-Tools is absent.
    from scheduler import build_shifts

    shifts, demand = build_shifts(config["rush_profile"])
    return {
        "raw_data": data,
        "scheduler_employees": employees,
        "config": config,
        "shifts": shifts,
        "demand": demand,
        "warnings": list(issues.warnings),
    }


def load_demo_input():
    """Load and validate the shipped demo workbook."""
    try:
        return load_workbook_input(TEMPLATE_PATH)
    except WorkbookValidationError as error:
        joined = "\n- ".join(error.errors)
        raise DemoInputError(f"Demo workbook validation failed:\n- {joined}", error.warnings) from error


def solve(inputs):
    """Call the real solver without asking the LLM or requesting an Excel export."""
    from scheduler import run_pipeline

    result = run_pipeline(inputs["scheduler_employees"], inputs["config"], xlsx_path=None, summary=None)
    if result.get("status") not in {"optimal", "feasible", "no_solution", "invalid_input"}:
        raise RuntimeError(f"Scheduler returned an unsupported result: {result.get('status')!r}")
    return result


def solve_workbook(path):
    """Load, solve, normalize, and explain an uploaded canonical workbook."""
    from time import perf_counter

    started = perf_counter()
    inputs = load_workbook_input(path)
    result = solve(inputs)
    schedule = normalize(result, inputs)
    schedule["data_source"] = "upload"
    explanation = build_explanation(schedule, inputs)
    return schedule, explanation, deepcopy(schedule["employees"]), inputs, perf_counter() - started


def normalize(result, inputs):
    """Map scheduler.py's result to the BonApp UI contract V2."""
    raw_data = inputs["raw_data"]
    status = result.get("status", "invalid_input")
    shifts = inputs["shifts"]
    task_names = list(raw_data.get("tasks", {}))
    summaries = {item["employee_id"]: item for item in result.get("employees", [])}
    assignment_rows = result.get("assignments", []) if status in {"optimal", "feasible"} else []
    missing_by_slot = {}
    for item in result.get("gaps", []):
        key = (item.get("shift"), item.get("role"))
        missing_by_slot[key] = item.get("missing", 0)

    demand_rows = []
    gaps = []
    for item in inputs["demand"]:
        window_key, task, needed = item["shift"], item["role"], item["needed"]
        missing = missing_by_slot.get((window_key, task), needed if status in {"no_solution", "invalid_input"} else 0)
        assigned = max(0, needed - missing)
        demand_rows.append({"window_key": window_key, "task": task, "needed": needed,
                            "assigned": assigned, "missing": missing})
        if missing:
            shift_info = shifts[window_key]
            gaps.append({"gap_id": f"{window_key}|{task}", "window_key": window_key,
                         "day": shift_info["day"], "window": shift_info["name"], "task": task,
                         "needed": needed, "assigned": assigned, "short_by": missing})

    windows = [{"key": key, "day": info["day"], "name": info["name"],
                "start": info["start"], "end": info["end"]}
               for key, info in shifts.items()]
    employees = []
    for person in raw_data.get("employees", []):
        skill_map = person.get("task_skills", {})
        rates = {}
        for task, skill in skill_map.items():
            rate = skill.get("hourly_rate")
            rates[task] = rate if rate is not None else inputs["config"]["default_role_rates"].get(task)
        summary = summaries.get(person["employee_id"], {})
        hours_scheduled = summary.get("hours", sum(row["hours"] for row in assignment_rows
                                                     if row["employee_id"] == person["employee_id"]))
        employees.append({
            "employee_id": person["employee_id"], "name": person["name"],
            "tasks": list(skill_map), "rates": rates,
            "min_hours": person.get("min_hours_week"),
            "max_hours": person.get("max_hours_week"),
            "hours_scheduled": hours_scheduled,
        })

    needed_total = sum(row["needed"] for row in demand_rows)
    missing_total = sum(row["missing"] for row in demand_rows)
    warnings = list(inputs.get("warnings", []))
    warnings.extend(item.get("message", str(item)) for item in result.get("warnings", []))
    warnings.extend(result.get("errors", []))
    return {
        "status": status,
        "data_source": "solver",
        "windows": windows,
        "tasks": task_names,
        "assignments": [{
            "employee_id": item["employee_id"], "name": item["employee_name"],
            "window_key": item["shift"], "day": item["day"], "window": item["window"],
            "start": item["start"], "end": item["end"], "task": item["role"],
            "hours": item["hours"], "wage": item["wage"], "cost": item["cost"],
        } for item in assignment_rows],
        "demand": demand_rows,
        "gaps": gaps,
        "employees": employees,
        "labor_cost": result.get("labor_cost", 0.0),
        "coverage_rate": (1 - missing_total / needed_total) if needed_total else 1.0,
        "warnings": warnings,
    }


def normalize_mock(warnings=None):
    """Convert the original UI mock fixtures to V2 for startup fallback."""
    employees_by_id = {person["employee_id"]: person for person in MOCK_EMPLOYEES}
    counts = {}
    windows_by_key = {}
    tasks = set()
    assignments = []
    for item in MOCK_SCHEDULE["assignments"]:
        window_key = f"{item['day']}_{item['shift'].title()}"
        key = (window_key, item["role"].title())
        counts[key] = counts.get(key, 0) + 1
        windows_by_key.setdefault(window_key, {
            "key": window_key, "day": item["day"], "name": item["shift"].title(),
            "start": item["start"], "end": item["end"],
        })
        task = item["role"].title()
        tasks.add(task)
        person = employees_by_id[item["employee_id"]]
        assignments.append({
            "employee_id": item["employee_id"], "name": item["name"], "window_key": window_key,
            "day": item["day"], "window": item["shift"].title(), "start": item["start"],
            "end": item["end"], "task": task, "hours": item["hours"],
            "wage": person["hourly_wage"], "cost": item["hours"] * person["hourly_wage"],
        })
    gaps = []
    for item in MOCK_SCHEDULE["gaps"]:
        window_key = f"{item['day']}_{item['shift'].title()}"
        key = (window_key, item["role"].title())
        counts[key] = item["assigned"]
        tasks.add(item["role"].title())
        windows_by_key.setdefault(window_key, {
            "key": window_key, "day": item["day"], "name": item["shift"].title(),
            "start": "11:00" if item["shift"] == "lunch" else "17:00",
            "end": "16:00" if item["shift"] == "lunch" else "22:00",
        })
        gaps.append({"gap_id": f"{window_key}|{item['role'].title()}", "window_key": window_key,
                     "day": item["day"], "window": item["shift"].title(), "task": item["role"].title(),
                     "needed": item["needed"], "assigned": item["assigned"], "short_by": item["short_by"]})
    demand = []
    for (window_key, task), assigned in counts.items():
        gap = next((item for item in gaps if item["window_key"] == window_key and item["task"] == task), None)
        needed = gap["needed"] if gap else assigned
        missing = gap["short_by"] if gap else 0
        demand.append({"window_key": window_key, "task": task, "needed": needed,
                       "assigned": assigned, "missing": missing})
    employees = []
    for person in MOCK_EMPLOYEES:
        person_assignments = [item for item in assignments if item["employee_id"] == person["employee_id"]]
        emp_tasks = [role.title() for role in person["roles"]]
        employees.append({
            "employee_id": person["employee_id"], "name": person["name"], "tasks": emp_tasks,
            "rates": {task: person["hourly_wage"] for task in emp_tasks},
            "min_hours": None, "max_hours": person["max_weekly_hours"],
            "hours_scheduled": sum(item["hours"] for item in person_assignments),
        })
    needed_total = sum(item["needed"] for item in demand)
    missing_total = sum(item["missing"] for item in demand)
    return {
        "status": "optimal", "data_source": "mock",
        "windows": list(windows_by_key.values()), "tasks": sorted(tasks),
        "assignments": assignments, "demand": demand, "gaps": gaps, "employees": employees,
        "labor_cost": MOCK_SCHEDULE["total_cost"],
        "coverage_rate": (1 - missing_total / needed_total) if needed_total else 1.0,
        "warnings": list(warnings or []),
    }


def build_explanation(schedule, inputs=None):
    """Code-only explanation with real solver candidate options when available."""
    employees = schedule.get("employees", [])
    most_loaded = max(employees, key=lambda item: item.get("hours_scheduled", 0), default=None)
    load_text = (f"{most_loaded['name']} is most loaded at {most_loaded['hours_scheduled']:g} of "
                 f"{most_loaded['max_hours']:g} hours." if most_loaded else "No employee hours are scheduled.")
    summary = (f"Labor cost is ${schedule.get('labor_cost', 0):,.2f}; coverage is "
               f"{schedule.get('coverage_rate', 1) * 100:.1f}% with {len(schedule.get('gaps', []))} gaps. {load_text}")

    candidate_map = {}
    if inputs and inputs.get("scheduler_employees") and inputs.get("shifts"):
        try:
            from pipeline import gap_candidates

            current = {
                "assignments": [{"employee_id": item["employee_id"], "shift": item["window_key"], "role": item["task"]}
                                for item in schedule["assignments"]],
                "employees": [{"employee_id": item["employee_id"], "hours": item["hours_scheduled"],
                               "shifts": sum(1 for row in schedule["assignments"] if row["employee_id"] == item["employee_id"])}
                              for item in employees],
                "gaps": [{"shift": gap["window_key"], "role": gap["task"], "missing": gap["short_by"]}
                         for gap in schedule["gaps"]],
            }
            for item in gap_candidates(current, inputs["scheduler_employees"], inputs["shifts"], limit=3):
                candidate_map[(item["shift"], item["role"])] = item["candidates"]
        except Exception:
            candidate_map = {}

    raw_people = {item["employee_id"]: item for item in (inputs or {}).get("raw_data", {}).get("employees", [])}
    windows = {item["key"]: item for item in schedule.get("windows", [])}
    employees_by_name = {item["name"]: item for item in employees}
    details = []
    for gap in schedule.get("gaps", []):
        window = windows.get(gap["window_key"], {})
        candidates = candidate_map.get((gap["window_key"], gap["task"]))
        if candidates is None:
            candidates = []
            for person in employees:
                if gap["task"] not in person["tasks"]:
                    continue
                assigned = any(item["employee_id"] == person["employee_id"] and item["window_key"] == gap["window_key"]
                               for item in schedule["assignments"])
                reason = "already working this window" if assigned else "available to consider"
                candidates.append({"name": person["name"], "reason": reason})
            candidates.sort(key=lambda item: (item["reason"] == "already working this window",
                                              employees_by_name[item["name"]]["hours_scheduled"]))
            candidates = candidates[:3]
        shift_info = (inputs or {}).get("shifts", {}).get(gap["window_key"], {})
        hours_to_add = (shift_info.get("minutes", 0) / 60) if shift_info else _hours(window.get("start"), window.get("end"))
        actions = []
        needs_approval = False
        for candidate in candidates[:3]:
            person = employees_by_name.get(candidate["name"])
            if not person:
                continue
            new_hours = person["hours_scheduled"] + hours_to_add
            max_hours = person.get("max_hours")
            raw = raw_people.get(person["employee_id"], {})
            threshold = raw.get("overtime_threshold_hours")
            exceeds = (max_hours is not None and new_hours > max_hours) or (threshold is not None and new_hours > threshold)
            needs_approval = needs_approval or exceeds
            cap_text = f"{person['hours_scheduled']:g}h / {max_hours:g}h max" if max_hours is not None else f"{person['hours_scheduled']:g}h scheduled"
            actions.append(f"{candidate['name']} ({cap_text}; {candidate['reason']})")
        suggested = ("Potential covers: " + "; ".join(actions) + ".") if actions else "No rostered employee is listed for this task."
        details.append({
            "gap_id": gap["gap_id"],
            "explanation": f"{gap['window']} on {gap['day']} is short {gap['short_by']} {gap['task']}.",
            "suggested_action": suggested,
            "needs_manager_approval": needs_approval,
        })
    return {"summary": summary, "gap_explanations": details}


def _hours(start, end):
    if not start or not end:
        return 0
    start_hour, start_min = map(int, start.split(":"))
    end_hour, end_min = map(int, end.split(":"))
    return (end_hour * 60 + end_min - start_hour * 60 - start_min) / 60


def initialize_demo():
    """Build the cached solver data once at service startup, falling back on errors."""
    from time import perf_counter

    started = perf_counter()
    try:
        inputs = load_demo_input()
        raw = solve(inputs)
        schedule = normalize(raw, inputs)
        explanation = build_explanation(schedule, inputs)
        employees = deepcopy(schedule["employees"])
    except Exception as error:
        warnings = list(getattr(error, "warnings", []))
        warnings.insert(0, f"Real solver unavailable; using mock data. {error}")
        schedule = normalize_mock(warnings)
        explanation = build_explanation(schedule)
        employees = deepcopy(schedule["employees"])
        inputs = None
    elapsed = perf_counter() - started
    source = schedule["data_source"]
    print(f"[BonApp] Demo schedule initialization: {elapsed:.3f}s ({source}).")
    return schedule, explanation, employees, inputs, elapsed
