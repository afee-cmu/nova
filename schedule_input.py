"""
schedule_input.py — accept and normalize the manager's uploaded schedule workbook,
and adapt it into the inputs scheduler.py expects.

This is the "Data In" layer in front of scheduler.py (the CP-SAT optimizer):

    workbook.xlsx
        -> load_workbook_input()   # read + validate -> normalized dict
        -> to_scheduler_inputs()   # adapt -> (employees, config) for scheduler.run_pipeline

Alignment with scheduler.py (by design):
  * A TASK here is the scheduler's "role" (a station: Fry Station, Register, ...).
    The broad Roles tab (Cook/Server) is HR grouping only and is not sent to the solver.
  * Pay is per task: each task has a default wage; a person's per-task wage (on the
    Proficiency tab) overrides it. This maps to scheduler's default_role_rates plus each
    employee's roles -> wage-or-null.
  * A person's Proficiency rows are their ELIGIBILITY: the set of tasks they can be given.
    The 1-5 proficiency score is recorded but not yet used by the optimizer (planned).
  * Availability is entered as time ranges; the adapter converts them to the named shifts
    scheduler builds from the rush profile (an employee can work a shift only if a block
    fully covers that window on that day).
  * Overtime is never scheduled: max_hours_week becomes the scheduler's hard max_hours and
    unmet demand surfaces as a gap for the manager. The Policy tab records the manager's
    overtime stance; it is not applied automatically.

CLI:
    python3 schedule_input.py [workbook.xlsx]   # validate + show scheduler-input summary
"""
from __future__ import annotations

import os
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import datetime

import openpyxl

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
EMP_TYPES = {"Full-time", "Part-time"}
PREFS = {"Preferred", "Available", "Avoid"}
REQUIRED_SHEETS = ["Roles", "Tasks", "Employees", "Availability",
                   "Proficiency", "RushWindows", "Policy"]

# Guards for UNTRUSTED uploads: an .xlsx is a zip, so a small file can expand into
# gigabytes ("zip bomb") and exhaust memory the moment openpyxl loads it. Check the
# zip's declared sizes BEFORE loading, and reject anything implausible for a roster.
MAX_UPLOAD_BYTES = 15 * 1024 * 1024          # 15 MB on disk
MAX_UNCOMPRESSED_BYTES = 200 * 1024 * 1024   # 200 MB decompressed across all parts
MAX_COMPRESSION_RATIO = 200                  # decompressed / compressed


def preflight_upload(path):
    """Cheap safety check on an untrusted .xlsx before openpyxl ever opens it.
    Returns an error string if the file is unsafe/implausible, else None."""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return f"cannot read file: {e}"
    if size > MAX_UPLOAD_BYTES:
        return f"file is {size // 1024 // 1024} MB; limit is {MAX_UPLOAD_BYTES // 1024 // 1024} MB"
    if not zipfile.is_zipfile(path):
        return "not a valid .xlsx file (an .xlsx is a zip archive)"
    try:
        with zipfile.ZipFile(path) as z:
            total = sum(i.file_size for i in z.infolist())
            comp = sum(i.compress_size for i in z.infolist()) or 1
    except zipfile.BadZipFile as e:
        return f"corrupt .xlsx: {e}"
    if total > MAX_UNCOMPRESSED_BYTES:
        return (f"file expands to {total // 1024 // 1024} MB, over the "
                f"{MAX_UNCOMPRESSED_BYTES // 1024 // 1024} MB limit — refusing (possible zip bomb)")
    if total / comp > MAX_COMPRESSION_RATIO:
        return f"suspicious compression ratio ({total // comp}x) — refusing (possible zip bomb)"
    return None

# Exact example rows shipped in the template; skip a row ONLY if it still matches its
# example exactly, so any edit turns it into real data (nothing is silently dropped).
_EX_EMPLOYEE = ("E001", "Jordan Smith", "Cook", "Dishwasher", "Full-time", 20, 40, 6, 40)
_EX_AVAIL = ("E001", "Mon", "09:00", "17:00", "Preferred")
_EX_PROF = ("E001", "Fry Station", 5, None, 150)
_EX_RUSH = ("Fri", "Dinner Rush", "18:00", "21:00", "Floor", 4)

EMP_HEADERS = ["employee_id", "name", "primary_role", "secondary_roles",
               "employment_type", "min_hours_week", "max_hours_week",
               "max_shifts_week", "overtime_threshold_hours"]
AV_HEADERS = ["employee_id", "day", "available_start", "available_end", "preference"]
PF_HEADERS = ["employee_id", "task", "proficiency", "hourly_rate", "throughput_per_hour"]
RW_HEADERS = ["day", "window_label", "window_start", "window_end", "task", "required_staff"]


@dataclass
class Issues:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def error(self, msg): self.errors.append(msg)
    def warn(self, msg): self.warnings.append(msg)
    def has_errors(self): return bool(self.errors)


def _parse_time(val, where, issues):
    if val is None or val == "":
        return None
    if isinstance(val, datetime):
        return val.hour * 60 + val.minute
    if hasattr(val, "hour") and hasattr(val, "minute"):
        return val.hour * 60 + val.minute
    s = str(val).strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            t = datetime.strptime(s, fmt)
            return t.hour * 60 + t.minute
        except ValueError:
            continue
    issues.error(f"{where}: cannot read time '{val}' (use 24h HH:MM, e.g. 09:00)")
    return None


def _hhmm(minutes):
    return None if minutes is None else f"{minutes // 60:02d}:{minutes % 60:02d}"


def _rows(ws):
    header = [c.value for c in ws[1]]
    for r_idx, row in enumerate(ws.iter_rows(min_row=2, values_only=True), start=2):
        if all(v is None or str(v).strip() == "" for v in row):
            continue
        yield r_idx, dict(zip(header, row))


def _matches_example(row, headers, example):
    for key, ex in zip(headers, example):
        got = row.get(key)
        if got is None:
            if ex is None:
                continue
            return False
        if ex is None:
            return False
        try:
            if float(got) == float(ex):
                continue
        except (TypeError, ValueError):
            pass
        if str(got).strip() != str(ex).strip():
            return False
    return True


def _num(row, key, where, issues, default=None, required=False):
    v = row.get(key)
    if v is None or str(v).strip() == "":
        if required:
            issues.error(f"{where}: '{key}' is required")
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        issues.error(f"{where}: '{key}' = '{v}' is not a number")
        return default


# ---------------------------------------------------------------------------
# Load + validate
# ---------------------------------------------------------------------------
def load_workbook_input(path):
    issues = Issues()

    unsafe = preflight_upload(path)   # guard untrusted uploads BEFORE loading
    if unsafe:
        issues.error(f"Rejected upload: {unsafe}")
        return None, issues

    wb = openpyxl.load_workbook(path, data_only=True)

    for s in REQUIRED_SHEETS:
        if s not in wb.sheetnames:
            issues.error(f"Missing required tab: '{s}'")
    if issues.has_errors():
        return None, issues

    # ---- Roles (HR grouping only) ----
    roles = {}
    for r, row in _rows(wb["Roles"]):
        name = str(row.get("role", "") or "").strip()
        if name:
            roles[name] = str(row.get("description", "") or "").strip()
    if not roles:
        issues.error("Roles tab is empty — define at least one role.")

    # ---- Tasks (scheduler roles; carry default wage) ----
    tasks = {}
    for r, row in _rows(wb["Tasks"]):
        name = str(row.get("task", "") or "").strip()
        if not name:
            continue
        where = f"Tasks row {r}"
        role = str(row.get("role", "") or "").strip()
        if role and role not in roles:
            issues.error(f"{where}: role '{role}' is not on the Roles tab")
        rate = _num(row, "default_hourly_rate", where, issues)
        if rate is not None and rate <= 0:
            issues.error(f"{where}: default_hourly_rate must be greater than 0")
        tasks[name] = {
            "task": name,
            "role": role,
            "description": str(row.get("description", "") or "").strip(),
            "default_hourly_rate": rate,
            "target_units_per_hour": _num(row, "target_units_per_hour", where, issues),
        }
    if not tasks:
        issues.error("Tasks tab is empty — define at least one task/station.")

    # ---- Employees ----
    employees = {}
    for r, row in _rows(wb["Employees"]):
        if _matches_example(row, EMP_HEADERS, _EX_EMPLOYEE):
            continue
        eid = str(row.get("employee_id", "") or "").strip()
        where = f"Employees row {r}"
        if not eid:
            issues.error(f"{where}: missing employee_id")
            continue
        if eid in employees:
            issues.error(f"{where}: duplicate employee_id '{eid}'")
            continue

        primary = str(row.get("primary_role", "") or "").strip()
        secondary = [s.strip() for s in str(row.get("secondary_roles", "") or "").split(",") if s.strip()]
        for role in [primary, *secondary]:
            if role and role not in roles:
                issues.error(f"{where}: role '{role}' is not on the Roles tab")

        emp_type = str(row.get("employment_type", "") or "").strip()
        if emp_type and emp_type not in EMP_TYPES:
            issues.warn(f"{where}: employment_type '{emp_type}' not one of {sorted(EMP_TYPES)}")

        min_h = _num(row, "min_hours_week", where, issues, default=0)
        max_h = _num(row, "max_hours_week", where, issues)
        if max_h is None:
            issues.error(f"{where}: max_hours_week is required")
        if max_h is not None and min_h is not None and min_h > max_h:
            issues.error(f"{where}: min_hours_week ({min_h}) exceeds max_hours_week ({max_h})")
        max_shifts = _num(row, "max_shifts_week", where, issues)

        employees[eid] = {
            "employee_id": eid,
            "name": str(row.get("name", "") or "").strip(),
            "roles": [r_ for r_ in [primary, *secondary] if r_],   # HR grouping
            "primary_role": primary,
            "employment_type": emp_type,
            "min_hours_week": min_h,
            "max_hours_week": max_h,
            "max_shifts_week": max_shifts,
            # Reference only — does NOT authorize overtime (that is the manager's call).
            "overtime_threshold_hours": _num(row, "overtime_threshold_hours", where, issues),
            "availability": [],
            # task -> {proficiency, hourly_rate, throughput_per_hour}; also the eligibility set.
            "task_skills": {},
        }
    if not employees:
        issues.error("No employees found — fill in the Employees tab.")

    # ---- Availability ----
    availability = []
    for r, row in _rows(wb["Availability"]):
        if _matches_example(row, AV_HEADERS, _EX_AVAIL):
            continue
        eid = str(row.get("employee_id", "") or "").strip()
        where = f"Availability row {r}"
        if not eid:
            issues.error(f"{where}: missing employee_id")
            continue
        if eid not in employees:
            issues.error(f"{where}: employee_id '{eid}' has no matching Employees row")
        day = str(row.get("day", "") or "").strip()
        if day not in DAYS:
            issues.error(f"{where}: day '{day}' must be one of {DAYS}")
        start = _parse_time(row.get("available_start"), f"{where} available_start", issues)
        end = _parse_time(row.get("available_end"), f"{where} available_end", issues)
        if start is not None and end is not None and start >= end:
            issues.error(f"{where}: available_start must be before available_end")
        pref = str(row.get("preference", "") or "").strip() or "Available"
        if pref not in PREFS:
            issues.warn(f"{where}: preference '{pref}' not one of {sorted(PREFS)}")
        block = {"employee_id": eid, "day": day, "start_min": start,
                 "end_min": end, "preference": pref}
        availability.append(block)
        if eid in employees:
            employees[eid]["availability"].append(block)

    # ---- Proficiency (eligibility + per-task wage + score-for-later) ----
    proficiency = []
    for r, row in _rows(wb["Proficiency"]):
        if _matches_example(row, PF_HEADERS, _EX_PROF):
            continue
        eid = str(row.get("employee_id", "") or "").strip()
        where = f"Proficiency row {r}"
        if not eid:
            issues.error(f"{where}: missing employee_id")
            continue
        if eid not in employees:
            issues.error(f"{where}: employee_id '{eid}' has no matching Employees row")
        task = str(row.get("task", "") or "").strip()
        if task not in tasks:
            issues.error(f"{where}: task '{task}' is not on the Tasks tab")
        score = _num(row, "proficiency", where, issues, required=True)
        if score is not None and not (1 <= score <= 5):
            issues.error(f"{where}: proficiency {score} must be between 1 and 5")
        wage = _num(row, "hourly_rate", where, issues)
        if wage is not None and wage <= 0:
            issues.error(f"{where}: hourly_rate must be greater than 0")
        thr = _num(row, "throughput_per_hour", where, issues)
        entry = {"employee_id": eid, "task": task, "proficiency": score,
                 "hourly_rate": wage, "throughput_per_hour": thr}
        proficiency.append(entry)
        if eid in employees and task in tasks:
            employees[eid]["task_skills"][task] = {
                "proficiency": score, "hourly_rate": wage, "throughput_per_hour": thr}

    # ---- RushWindows (demand, per task) ----
    demand = []
    for r, row in _rows(wb["RushWindows"]):
        if _matches_example(row, RW_HEADERS, _EX_RUSH):
            continue
        where = f"RushWindows row {r}"
        day = str(row.get("day", "") or "").strip()
        if day not in DAYS:
            issues.error(f"{where}: day '{day}' must be one of {DAYS}")
        task = str(row.get("task", "") or "").strip()
        if not task:
            issues.error(f"{where}: task is required (demand is tracked per task/station)")
        elif task not in tasks:
            issues.error(f"{where}: task '{task}' is not on the Tasks tab")
        elif tasks[task]["default_hourly_rate"] is None:
            issues.error(f"{where}: task '{task}' is demanded but has no default_hourly_rate "
                         "on the Tasks tab (the optimizer needs a wage to staff it)")
        label = str(row.get("window_label", "") or "").strip()
        if not label:
            issues.error(f"{where}: window_label is required")
        start = _parse_time(row.get("window_start"), f"{where} window_start", issues)
        end = _parse_time(row.get("window_end"), f"{where} window_end", issues)
        if start is not None and end is not None and start >= end:
            issues.error(f"{where}: window_start must be before window_end")
        req = row.get("required_staff")
        try:
            req = int(req) if req is not None and str(req).strip() != "" else None
        except (TypeError, ValueError):
            issues.error(f"{where}: required_staff '{req}' is not a whole number")
            req = None
        if req is not None and req < 0:
            issues.error(f"{where}: required_staff cannot be negative")
        # Optional busy-time weight: higher priority => the efficiency optimizer works
        # harder to place its most efficient people in this window (to cut wait times).
        # Optional column; defaults to 1 (normal) when blank or absent.
        priority = _num(row, "priority", where, issues, default=1)
        if priority is not None and priority < 1:
            issues.error(f"{where}: priority must be 1 or greater (1 = normal)")
        demand.append({"day": day, "window_label": label, "start_min": start,
                       "end_min": end, "task": task,
                       "role": tasks.get(task, {}).get("role", ""), "required_staff": req,
                       "priority": priority if priority is not None else 1})

    if not demand:
        issues.warn("No rush windows defined — the optimizer will have no demand to staff.")

    # ---- same (day, window_label) must agree on its time window ----
    win_times = {}
    for d in demand:
        if d["start_min"] is None or d["end_min"] is None:
            continue
        key = (d["day"], d["window_label"])
        span = (d["start_min"], d["end_min"])
        if key in win_times and win_times[key] != span:
            issues.error(f"Window '{d['window_label']}' on {d['day']} has conflicting times "
                         f"({_hhmm(win_times[key][0])}-{_hhmm(win_times[key][1])} vs "
                         f"{_hhmm(span[0])}-{_hhmm(span[1])}) — one window, one time.")
        else:
            win_times.setdefault(key, span)

    # ---- Policy (manager decisions; overtime lives here) ----
    policy_raw = {}
    for r, row in _rows(wb["Policy"]):
        key = str(row.get("setting", "") or "").strip()
        if key:
            policy_raw[key] = row.get("value")
    ot = str(policy_raw.get("overtime_allowed", "") or "").strip().lower()
    if ot not in ("yes", "no", ""):
        issues.warn(f"Policy: overtime_allowed = '{policy_raw.get('overtime_allowed')}' "
                    "should be Yes or No; treating as No.")
    policy = {
        "overtime_allowed": ot == "yes",
        "max_overtime_hours_per_employee_week": policy_raw.get("max_overtime_hours_per_employee_week"),
        "overtime_requires_approval": True,
        "scheduling_objective": str(policy_raw.get("scheduling_objective", "") or "").strip(),
        "raw": policy_raw,
    }

    # ---- optimization-aware sanity checks (flag, don't block) ----
    eligible_tasks = {p["task"] for p in proficiency if p.get("task") in tasks}
    for d in demand:
        if d["task"] in tasks and d["required_staff"] and d["task"] not in eligible_tasks:
            issues.warn(f"Demand for task '{d['task']}' ({d['day']} {d['window_label']}) but no "
                        "employee is listed as able to do it on the Proficiency tab.")
    for e in employees.values():
        if not e["task_skills"]:
            issues.warn(f"Employee {e['employee_id']} ({e['name']}) has no Proficiency rows, "
                        "so the optimizer can never assign them.")

    data = {
        "roles": roles,
        "tasks": tasks,
        "employees": list(employees.values()),
        "availability": availability,
        "proficiency": proficiency,
        "demand": demand,
        "policy": policy,
    }
    return data, issues


# ---------------------------------------------------------------------------
# Adapt to scheduler.py's inputs
# ---------------------------------------------------------------------------
def _role(task):
    """Scheduler roles are lowercase (matching pipeline.py's convention). The workbook
    keeps human-friendly task names (e.g. 'Fry Station'); the solver sees 'fry station'."""
    return task.strip().lower()


def to_scheduler_inputs(data):
    """Convert normalized `data` into (employees, config) for scheduler.run_pipeline.

    - config.default_role_rates : {role: default wage}  (tasks that have a wage)
    - config.rush_profile       : {day: [{name, window, staff:{role: n}}]}  (per-day)
    - employees[*].roles        : {role: per-person wage or None}  (from Proficiency)
    - employees[*].available    : ["<Day>_<window_label>"] covered by availability blocks
    - employees[*].max_hours    : max_hours_week   (hard cap; no overtime)
    - employees[*].max_shifts   : max_shifts_week  (omitted if blank)

    Task names are lowercased to roles (see _role) so this path and pipeline.py agree.
    """
    config = {
        "default_role_rates": {_role(name): t["default_hourly_rate"]
                               for name, t in data["tasks"].items()
                               if t["default_hourly_rate"] is not None},
        "rush_profile": _build_rush_profile(data["demand"]),
    }

    # shift windows: (day, label) -> (start_min, end_min); names match scheduler's "<day>_<name>"
    windows = {}
    for d in data["demand"]:
        if d["start_min"] is not None and d["end_min"] is not None:
            windows.setdefault((d["day"], d["window_label"]), (d["start_min"], d["end_min"]))

    employees = []
    for e in data["employees"]:
        avail_by_day = {}
        for b in e["availability"]:
            if b["start_min"] is not None and b["end_min"] is not None:
                avail_by_day.setdefault(b["day"], []).append((b["start_min"], b["end_min"]))
        available = []
        for (day, label), (ws_, we_) in windows.items():
            blocks = avail_by_day.get(day, [])
            if any(bs <= ws_ and be >= we_ for bs, be in blocks):   # block fully covers window
                available.append(f"{day}_{label}")

        emp = {
            "id": e["employee_id"],
            "name": e["name"],
            # eligibility + wage per task; None wage -> scheduler uses the task default
            "roles": {_role(task): sk["hourly_rate"] for task, sk in e["task_skills"].items()},
            "max_hours": e["max_hours_week"] if e["max_hours_week"] is not None else 0,
            "available": available,
        }
        # scheduler.py ignores unknown keys; reoptimize.py uses min_hours for rights flags
        if e["min_hours_week"]:
            emp["min_hours"] = e["min_hours_week"]
        if e["max_shifts_week"] is not None:
            emp["max_shifts"] = e["max_shifts_week"]
        employees.append(emp)

    return employees, config


def _build_rush_profile(demand):
    """Group demand rows into scheduler's per-day rush_profile."""
    by_day_label = {}   # (day, label) -> {"window": "HH:MM-HH:MM", "staff": {task: n}}
    for d in demand:
        if not d["task"] or d["start_min"] is None or d["end_min"] is None:
            continue
        key = (d["day"], d["window_label"])
        entry = by_day_label.setdefault(
            key, {"name": d["window_label"],
                  "window": f"{_hhmm(d['start_min'])}-{_hhmm(d['end_min'])}",
                  "staff": {}})
        if d["required_staff"]:
            role = _role(d["task"])
            entry["staff"][role] = entry["staff"].get(role, 0) + d["required_staff"]

    profile = {}
    for (day, _label), entry in by_day_label.items():
        profile.setdefault(day, []).append(entry)
    return profile


# ---------------------------------------------------------------------------
# Efficiency inputs (for efficiency_scheduler, not the cost scheduler)
# ---------------------------------------------------------------------------
# Which broad role sits front-of-house vs back-of-house. The efficiency optimizer
# keeps people in their domain (a server is never put on the grill), so this is the
# hard FOH/BOH boundary. "any" (e.g. Manager) may work either side.
DOMAIN_BY_ROLE = {
    "cook": "back", "dishwasher": "back", "prep": "back", "line": "back",
    "server": "front", "cashier": "front", "host": "front", "bar": "front", "runner": "front",
    "manager": "any",
}


def _domain(role_name):
    return DOMAIN_BY_ROLE.get(str(role_name).strip().lower(), "any")


def efficiency_inputs(data):
    """Build inputs for efficiency_scheduler.solve_efficient_schedule from workbook data.

    Returns (employees, config, extras) where extras holds:
      - efficiency   : {(employee_id, role): rating}   higher = more efficient
      - role_domain  : {role: "front"|"back"|"any"}    (role = lowercased task)
      - window_weight: {shift_name: weight}            busy windows weigh more

    Efficiency rating per (person, task) comes from the Proficiency tab (the 1-5 score;
    a future UI may derive it from a time metric and map it onto the same higher=better
    scale). FOH/BOH domain comes from each task's broad role. Busy-window weight comes
    from the RushWindows 'priority' column (default 1). Pay is NOT part of this path.
    """
    employees, config = to_scheduler_inputs(data)

    # tag each employee with their domain (from their primary/first role)
    by_id = {e["employee_id"]: e for e in data["employees"]}
    for emp in employees:
        src = by_id.get(emp["id"], {})
        role = src.get("primary_role") or (src.get("roles") or [None])[0]
        emp["domain"] = _domain(role) if role else "any"

    # role (= lowercased task) -> domain, from each task's broad role
    role_domain = {_role(name): _domain(t["role"]) for name, t in data["tasks"].items()}

    # efficiency rating per (employee, role); role is the lowercased task name
    efficiency = {}
    for src in data["employees"]:
        for task, sk in src["task_skills"].items():
            if sk.get("proficiency") is not None:
                efficiency[(src["employee_id"], _role(task))] = float(sk["proficiency"])

    # busy-window weight: max priority among a window's demand rows (default 1)
    window_weight = {}
    for d in data["demand"]:
        if d["task"] and d["start_min"] is not None and d["end_min"] is not None:
            shift = f"{d['day']}_{d['window_label']}"
            window_weight[shift] = max(window_weight.get(shift, 1), int(d.get("priority") or 1))

    extras = {"efficiency": efficiency, "role_domain": role_domain, "window_weight": window_weight}
    return employees, config, extras


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "schedule_input_template.xlsx"
    data, issues = load_workbook_input(path)
    print(f"Reading: {path}\n")
    if issues.errors:
        print(f"ERRORS ({len(issues.errors)}) — must fix before scheduling:")
        for e in issues.errors:
            print(f"  x {e}")
    if issues.warnings:
        print(f"\nWARNINGS ({len(issues.warnings)}):")
        for w in issues.warnings:
            print(f"  ! {w}")
    if data and not issues.has_errors():
        emps, config = to_scheduler_inputs(data)
        print("OK — input is valid.\n")
        print(f"  tasks (scheduler roles): {', '.join(data['tasks'])}")
        print(f"  default_role_rates:      {config['default_role_rates']}")
        print(f"  employees:               {len(emps)}")
        print(f"  rush days:               {', '.join(config['rush_profile'])}")
        print(f"  overtime:                allowed={data['policy']['overtime_allowed']} "
              "(manager-decided; never auto-scheduled)")
        print("\nAdapter output ready for scheduler.run_pipeline(employees, config).")
    elif not issues.errors:
        print("No data loaded.")
