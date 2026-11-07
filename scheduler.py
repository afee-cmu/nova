"""
Nova scheduler
==============
Turns employee profiles + a restaurant's rush profile into an optimized weekly
schedule (Google OR-Tools CP-SAT), then exports it to Excel.

Pipeline:  build_shifts -> validate_input -> build_schedule -> export_schedule_xlsx
Entry point for a future API/web app:  run_pipeline(employees, config)

---------------------------------------------------------------------------
DATA MODEL (each record maps cleanly onto a future database table / profile)
---------------------------------------------------------------------------

EmployeeProfile  (one per employee; becomes a row in an `employees` table)
{
  "id": "emp_001",              # stable unique id (primary key later)
  "name": "Alex Kim",           # display name
  "roles": {                    # roles this employee can work -> THEIR hourly wage for it
    "grill": 19.0,              #   number = this employee's rate for this role
    "prep": null                #   null   = use the restaurant's default rate for the role
  },
  "min_hours": 12,              # optional weekly minimum (soft: warned about if impossible)
  "max_hours": 30,              # weekly hour cap
  "max_shifts": 5,              # optional weekly shift cap
  "available": ["Fri_dinner", "Sat_lunch"]   # shift names are "<Day>_<window name>"
}

RestaurantConfig  (one per restaurant/location)
{
  "default_role_rates": {"grill": 17, "cashier": 14, "expo": 15},   # also defines valid roles
  "rush_profile": {
    "weekday": [ {"name": "lunch",  "window": "11:00-14:00", "staff": {"grill": 1, "cashier": 1}},
                 {"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 2, "cashier": 1, "expo": 1}} ],
    "weekend": [ ... ],
    "Fri":     [ ... ]          # optional: a specific day overrides its weekday/weekend group
  },
  "everyone_gets_a_shift": true # optional (default true): try to give every available employee >= 1 shift
}

Wages: an employee's own rate for a role wins; otherwise the restaurant default for
that role applies. So pay can differ by role AND by employee.

Priorities, highest first (the solver trades off in this order):
  1. fill every slot the rush profile asks for
  2. give every employee with availability at least one shift
  3. meet each employee's min_hours
  4. minimize labor cost
Nobody is ever added beyond what a window needs just to give them hours; instead,
people below their minimum take slots that would otherwise go to someone else.
"""

from collections import defaultdict

from ortools.sat.python import cp_model

DAY_ORDER = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
DAY_GROUPS = {"weekday": ["Mon", "Tue", "Wed", "Thu", "Fri"], "weekend": ["Sat", "Sun"]}

# Cost of leaving one slot unfilled, in cents ($10,000). Always worse than paying
# anyone, so the solver fills every slot it legally can before leaving a gap.
SHORTAGE_PENALTY_CENTS = 1_000_000

# Penalty for an employee with availability getting zero shifts ($3,000): below an
# unfilled slot, above any amount of min-hours shortfall or wage difference.
UNSCHEDULED_PENALTY_CENTS = 300_000

# Penalty per minute an employee falls below their min_hours ($60/hour short): more
# than any wage difference between two people, so min-hours beats saving money.
MIN_HOURS_PENALTY_CENTS_PER_MIN = 100


# ---------------------------------------------------------------------------
# 1. Expand the rush profile into concrete shifts + staffing demand
# ---------------------------------------------------------------------------
def _to_minutes(hhmm):
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


def build_shifts(rush_profile):
    """Returns:
    shifts: {shift_name: {"day", "name", "start", "end", "minutes"}}
    demand: [{"shift", "role", "needed"}]
    """
    shifts, demand = {}, []
    for day in DAY_ORDER:
        group = "weekday" if day in DAY_GROUPS["weekday"] else "weekend"
        windows = rush_profile.get(day, rush_profile.get(group, []))
        for w in windows:
            start, end = [t.strip() for t in w["window"].split("-")]
            shift_name = f"{day}_{w['name']}"
            shifts[shift_name] = {
                "day": day,
                "name": w["name"],
                "start": start,
                "end": end,
                "minutes": _to_minutes(end) - _to_minutes(start),
            }
            for role, needed in w.get("staff", {}).items():
                if needed and needed > 0:
                    demand.append({"shift": shift_name, "role": role, "needed": int(needed)})
    return shifts, demand


# ---------------------------------------------------------------------------
# 2. Validate (catches LLM mistakes before anything runs)
# ---------------------------------------------------------------------------
def resolve_wage(employee, role, default_role_rates):
    """Employee's own rate for the role if set, otherwise the restaurant default."""
    rate = employee.get("roles", {}).get(role)
    return rate if rate is not None else default_role_rates.get(role)


def validate_input(employees, config, shifts, demand):
    """Returns a list of human-readable error strings. Empty list = OK to solve."""
    errors = []
    rates = config.get("default_role_rates", {})
    known_roles = set(rates)

    for name, info in shifts.items():
        if info["minutes"] <= 0:
            errors.append(f"Shift {name}: end time must be after start time ({info['start']}-{info['end']})")
    for d in demand:
        if d["role"] not in known_roles:
            errors.append(f"Shift {d['shift']}: unknown role '{d['role']}' (add it to default_role_rates)")

    seen_ids = set()
    for e in employees:
        label = e.get("name") or e.get("id") or "<unnamed employee>"
        for field in ("id", "name", "roles", "max_hours", "available"):
            if field not in e:
                errors.append(f"{label}: missing '{field}'")
        if e.get("id") in seen_ids:
            errors.append(f"{label}: duplicate id '{e.get('id')}'")
        seen_ids.add(e.get("id"))

        roles = e.get("roles", {})
        if not isinstance(roles, dict):
            errors.append(f"{label}: 'roles' must map role -> wage (or null)")
            continue
        for role in roles:
            if role not in known_roles:
                errors.append(f"{label}: unknown role '{role}'")
                continue
            wage = resolve_wage(e, role, rates)
            if not isinstance(wage, (int, float)) or wage <= 0:
                errors.append(f"{label}: no valid wage for role '{role}'")

        for s in e.get("available", []):
            if s not in shifts:
                errors.append(f"{label}: unknown shift '{s}'")
        for field in ("min_hours", "max_hours", "max_shifts"):
            if field in e and e[field] is not None:
                if not isinstance(e[field], (int, float)) or e[field] < 0:
                    errors.append(f"{label}: '{field}' must be a non-negative number")
        mn, mx = e.get("min_hours"), e.get("max_hours")
        if isinstance(mn, (int, float)) and isinstance(mx, (int, float)) and mn > mx:
            errors.append(f"{label}: min_hours ({mn}) is more than max_hours ({mx})")
    return errors


# ---------------------------------------------------------------------------
# 3. Solve
# ---------------------------------------------------------------------------
def build_schedule(employees, config, shifts, demand, time_limit_s=10):
    rates = config["default_role_rates"]
    emp = {e["id"]: e for e in employees}
    roles_needed = defaultdict(set)  # shift -> roles that shift actually needs
    for d in demand:
        roles_needed[d["shift"]].add(d["role"])

    m = cp_model.CpModel()

    # x[(employee_id, shift, role)] = 1 if that employee works that shift in that role.
    # Only created where the employee is available, can do the role, AND the shift needs it.
    x, cost_cents = {}, {}
    for eid, e in emp.items():
        available = set(e["available"])
        for s, info in shifts.items():
            if s not in available:
                continue
            for r in e["roles"]:
                if r in roles_needed[s]:
                    x[eid, s, r] = m.NewBoolVar(f"{eid}|{s}|{r}")
                    cost_cents[eid, s, r] = round(resolve_wage(e, r, rates) * info["minutes"] / 60 * 100)

    by_emp_shift = defaultdict(list)   # (eid, shift) -> vars
    by_emp = defaultdict(list)         # eid -> [(shift, var)]
    by_shift_role = defaultdict(list)  # (shift, role) -> vars
    for (eid, s, r), v in x.items():
        by_emp_shift[eid, s].append(v)
        by_emp[eid].append((s, v))
        by_shift_role[s, r].append(v)

    # One role per shift per employee
    for vs in by_emp_shift.values():
        m.Add(sum(vs) <= 1)

    # Weekly hour cap and optional shift cap
    for eid, items in by_emp.items():
        e = emp[eid]
        m.Add(sum(shifts[s]["minutes"] * v for s, v in items) <= int(e["max_hours"] * 60))
        if e.get("max_shifts") is not None:
            m.Add(sum(v for _, v in items) <= int(e["max_shifts"]))

    # Cover demand; `short` records gaps instead of making the model unsolvable.
    # Never staff a role above what the window needs.
    short = {}
    for d in demand:
        key = (d["shift"], d["role"])
        staffed = sum(by_shift_role.get(key, []))
        short[key] = m.NewIntVar(0, d["needed"], f"short|{d['shift']}|{d['role']}")
        m.Add(staffed + short[key] >= d["needed"])
        m.Add(staffed <= d["needed"])

    # Soft minimums: minutes below min_hours, and whether each person works at all
    under_min, works_any = {}, {}
    for eid, e in emp.items():
        items = by_emp.get(eid, [])
        min_minutes = int((e.get("min_hours") or 0) * 60)
        if min_minutes > 0:
            under_min[eid] = m.NewIntVar(0, min_minutes, f"under|{eid}")
            m.Add(sum(shifts[s]["minutes"] * v for s, v in items) + under_min[eid] >= min_minutes)
        if config.get("everyone_gets_a_shift", True) and items:
            works_any[eid] = m.NewBoolVar(f"works|{eid}")
            m.Add(sum(v for _, v in items) >= works_any[eid])

    # Objective, in priority order via penalty sizes (all in cents)
    m.Minimize(
        SHORTAGE_PENALTY_CENTS * sum(short.values())
        + UNSCHEDULED_PENALTY_CENTS * (len(works_any) - sum(works_any.values()))
        + MIN_HOURS_PENALTY_CENTS_PER_MIN * sum(under_min.values())
        + sum(cost_cents[k] * v for k, v in x.items())
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "no_solution"}

    assignments = []
    for (eid, s, r), v in x.items():
        if solver.Value(v):
            info = shifts[s]
            assignments.append({
                "employee_id": eid,
                "employee_name": emp[eid]["name"],
                "shift": s,
                "day": info["day"],
                "window": info["name"],
                "start": info["start"],
                "end": info["end"],
                "role": r,
                "hours": info["minutes"] / 60,
                "wage": resolve_wage(emp[eid], r, rates),
                "cost": cost_cents[eid, s, r] / 100,
            })
    assignments.sort(key=lambda a: (DAY_ORDER.index(a["day"]), a["start"], a["role"], a["employee_name"]))

    gaps = [
        {"shift": s, "day": shifts[s]["day"], "window": shifts[s]["name"],
         "role": r, "missing": solver.Value(v)}
        for (s, r), v in short.items() if solver.Value(v) > 0
    ]
    gaps.sort(key=lambda g: (DAY_ORDER.index(g["day"]), shifts[g["shift"]]["start"], g["role"]))

    summary = employee_summary(assignments, employees)
    return {
        "status": "optimal" if status == cp_model.OPTIMAL else "feasible",
        "assignments": assignments,
        "gaps": gaps,
        "labor_cost": round(sum(a["cost"] for a in assignments), 2),
        "employees": summary,
        "warnings": staffing_warnings(summary, employees, shifts, demand),
    }


def staffing_warnings(summary, employees, shifts, demand):
    """Explain anyone left unscheduled or below their minimum, with the reason."""
    needed = defaultdict(set)  # shift -> roles that shift needs
    for d in demand:
        needed[d["shift"]].add(d["role"])
    by_id = {e["id"]: e for e in employees}

    warnings = []
    for row in summary:
        e = by_id[row["employee_id"]]
        eligible = [s for s in e["available"] if needed[s] & set(e["roles"])]
        eligible_hours = sum(shifts[s]["minutes"] for s in eligible) / 60
        min_h = e.get("min_hours") or 0

        if row["shifts"] == 0:
            if not eligible:
                why = "none of their available shifts need their role(s)" if e["available"] else "they listed no availability"
            else:
                why = "every slot they could take was needed by someone filling a gap or meeting a minimum"
            warnings.append({"employee_id": e["id"], "name": e["name"], "type": "unscheduled",
                             "message": f"{e['name']} has no shifts this week: {why}."})
        elif row["hours"] < min_h:
            if eligible_hours < min_h:
                why = f"they're only available for {eligible_hours:g} hrs of shifts that need their role(s)"
            else:
                why = "the other shifts they could work are taken by people with higher priority"
            warnings.append({"employee_id": e["id"], "name": e["name"], "type": "below_min_hours",
                             "message": f"{e['name']} is scheduled {row['hours']:g} hrs, below their {min_h:g}-hr minimum: {why}."})
    return warnings


def employee_summary(assignments, employees):
    """Per-employee breakdown: what each profile page / pay estimate would show."""
    rows = []
    for e in employees:
        mine = [a for a in assignments if a["employee_id"] == e["id"]]
        assigned = {a["shift"] for a in mine}
        rows.append({
            "employee_id": e["id"],
            "name": e["name"],
            "shifts": len(mine),
            "hours": round(sum(a["hours"] for a in mine), 2),
            "min_hours": e.get("min_hours"),
            "max_hours": e["max_hours"],
            "max_shifts": e.get("max_shifts"),
            "pay": round(sum(a["cost"] for a in mine), 2),
            "roles_worked": sorted({a["role"] for a in mine}),
            "available_but_unassigned": [s for s in e["available"] if s not in assigned],
        })
    return rows


# ---------------------------------------------------------------------------
# 4. Export to Excel
# ---------------------------------------------------------------------------
# Excel/CSV formula injection: Excel and Google Sheets treat a cell whose text starts
# with = + - @ (or a leading tab/CR) as a FORMULA when the file is opened. Employee
# names and other free text flow into this export, so a name like '=HYPERLINK("http://evil",...)'
# would execute on open. Neutralize such cells with a leading apostrophe.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _excel_safe(v):
    return "'" + v if isinstance(v, str) and v[:1] in _FORMULA_LEAD else v


def _sanitize_df(df):
    # Map every cell (no-op on non-strings). Don't gate on dtype == object: newer pandas
    # gives string columns a dedicated 'str' dtype, which that check would skip.
    for col in df.columns:
        df[col] = df[col].map(_excel_safe)
    return df


def export_schedule_xlsx(result, path="schedule.xlsx", summary=None):
    """Writes Schedule (grid), Assignments, Employees, Gaps, Cost and optional Summary tabs.
    `summary` is the LLM's plain-English explanation; call again with it once you have it.
    All string cells are sanitized against spreadsheet formula injection, and the file is
    written owner-only (0600) because it contains employee PII."""
    import os
    import pandas as pd
    from openpyxl.styles import Alignment, Font

    # Row labels: name, or "name (id)" if two employees share a name
    names = [e["name"] for e in result["employees"]]
    label = {
        e["employee_id"]: e["name"] if names.count(e["name"]) == 1 else f"{e['name']} ({e['employee_id']})"
        for e in result["employees"]
    }

    grid = pd.DataFrame("", index=[_excel_safe(v) for v in label.values()], columns=DAY_ORDER)
    for a in result["assignments"]:
        row = _excel_safe(label[a["employee_id"]])
        line = f"{a['window']} {a['start']}-{a['end']} ({a['role']})"
        current = grid.loc[row, a["day"]]
        grid.loc[row, a["day"]] = f"{current}\n{line}" if current else line
    grid = _sanitize_df(grid)

    employees_df = pd.DataFrame(result["employees"])
    for col in ("roles_worked", "available_but_unassigned"):
        employees_df[col] = employees_df[col].apply(", ".join)

    gaps_df = pd.DataFrame(result["gaps"]) if result["gaps"] else pd.DataFrame([{"note": "No gaps"}])
    cost_df = pd.DataFrame([{"total_labor_cost": result["labor_cost"]}])

    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        grid.to_excel(writer, sheet_name="Schedule")
        _sanitize_df(pd.DataFrame(result["assignments"])).to_excel(writer, sheet_name="Assignments", index=False)
        _sanitize_df(employees_df).to_excel(writer, sheet_name="Employees", index=False)
        _sanitize_df(gaps_df).to_excel(writer, sheet_name="Gaps", index=False)
        cost_df.to_excel(writer, sheet_name="Cost", index=False)
        if result.get("warnings"):
            _sanitize_df(pd.DataFrame(result["warnings"])).to_excel(writer, sheet_name="Warnings", index=False)
        if summary:
            sdf = _sanitize_df(pd.DataFrame({"Summary": summary.split("\n")}))
            sdf.to_excel(writer, sheet_name="Summary", index=False)

        # Make the grid readable
        ws = writer.sheets["Schedule"]
        ws.column_dimensions["A"].width = 20
        for col in "BCDEFGH":
            ws.column_dimensions[col].width = 30
        for row in ws.iter_rows():
            for cell in row:
                cell.alignment = Alignment(wrap_text=True, vertical="top")
        for cell in ws[1]:
            cell.font = Font(bold=True)
    os.chmod(path, 0o600)   # employee PII: owner-only at rest
    return path


# ---------------------------------------------------------------------------
# Entry point for the app / API
# ---------------------------------------------------------------------------
def run_pipeline(employees, config, xlsx_path=None, summary=None):
    """Full pipeline. Returns either {"status": "invalid_input", "errors": [...]}
    or the schedule result (plus "xlsx_path" if an export path was given)."""
    missing = [k for k in ("default_role_rates", "rush_profile") if k not in config]
    if missing:
        return {"status": "invalid_input", "errors": [f"config missing '{k}'" for k in missing]}

    shifts, demand = build_shifts(config["rush_profile"])
    errors = validate_input(employees, config, shifts, demand)
    if errors:
        return {"status": "invalid_input", "errors": errors}

    result = build_schedule(employees, config, shifts, demand)
    if xlsx_path and result["status"] in ("optimal", "feasible"):
        result["xlsx_path"] = export_schedule_xlsx(result, xlsx_path, summary)
    return result


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    config = {
        "default_role_rates": {"grill": 17, "cashier": 14, "expo": 15},
        "rush_profile": {
            "weekday": [
                {"name": "lunch",  "window": "11:00-14:00", "staff": {"grill": 1, "cashier": 1}},
                {"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1, "cashier": 1, "expo": 1}},
            ],
            "Fri": [  # Friday is busier than other weekdays
                {"name": "lunch",  "window": "11:00-14:00", "staff": {"grill": 1, "cashier": 1}},
                {"name": "dinner", "window": "17:00-22:00", "staff": {"grill": 2, "cashier": 1, "expo": 1}},
            ],
            "weekend": [
                {"name": "lunch",  "window": "11:00-15:00", "staff": {"grill": 2, "cashier": 1, "expo": 1}},
                {"name": "dinner", "window": "17:00-22:00", "staff": {"grill": 2, "cashier": 2, "expo": 1}},
            ],
        },
    }

    employees = [
        {"id": "emp_001", "name": "Alex",   "roles": {"grill": 19},              "max_hours": 32, "max_shifts": 7,
         "available": ["Mon_dinner", "Tue_dinner", "Wed_dinner", "Fri_dinner", "Sat_lunch", "Sat_dinner", "Sun_dinner"]},
        {"id": "emp_002", "name": "Sam",    "roles": {"grill": 21, "expo": 17},  "max_hours": 40, "max_shifts": 8,
         "available": ["Mon_lunch", "Tue_lunch", "Thu_dinner", "Fri_dinner", "Sat_lunch", "Sat_dinner", "Sun_lunch", "Sun_dinner"]},
        {"id": "emp_003", "name": "Priya",  "roles": {"grill": None},            "max_hours": 25, "max_shifts": 5,
         "available": ["Wed_lunch", "Thu_lunch", "Fri_lunch", "Sun_lunch", "Thu_dinner"]},
        {"id": "emp_004", "name": "Jordan", "roles": {"cashier": None},          "max_hours": 20,
         "available": ["Mon_lunch", "Tue_lunch", "Wed_lunch", "Fri_dinner", "Sat_dinner", "Sun_dinner"]},
        {"id": "emp_005", "name": "Riley",  "roles": {"cashier": 15, "expo": 16}, "max_hours": 30,
         "available": ["Mon_dinner", "Tue_dinner", "Wed_dinner", "Thu_lunch", "Sat_lunch", "Sun_lunch", "Sat_dinner"]},
        {"id": "emp_006", "name": "Chris",  "roles": {"expo": None, "cashier": None}, "max_hours": 24,
         "available": ["Thu_dinner", "Fri_lunch", "Fri_dinner", "Sat_dinner", "Sun_lunch", "Sun_dinner"]},
        {"id": "emp_007", "name": "Dana",   "roles": {"cashier": None}, "min_hours": 12, "max_hours": 20,
         "available": ["Mon_lunch", "Tue_lunch", "Wed_lunch", "Thu_lunch", "Fri_lunch"]},
        {"id": "emp_008", "name": "Morgan", "roles": {"expo": None}, "min_hours": 10, "max_hours": 20,
         "available": ["Mon_lunch", "Tue_lunch"]},   # weekday lunches need no expo -> can't be scheduled
    ]

    result = run_pipeline(employees, config, xlsx_path="schedule.xlsx")

    if result["status"] == "invalid_input":
        print("Input errors:")
        for err in result["errors"]:
            print("  -", err)
    else:
        print(f"Status: {result['status']}   Total labor cost: ${result['labor_cost']:.2f}\n")
        print("Per-employee:")
        for e in result["employees"]:
            print(f"  {e['name']:<7} {e['shifts']} shifts, {e['hours']:>4} hrs (min {e['min_hours'] or 0}, max {e['max_hours']}), "
                  f"${e['pay']:.2f} | roles: {', '.join(e['roles_worked']) or '-'} | "
                  f"available but unassigned: {', '.join(e['available_but_unassigned']) or '-'}")
        print("\nGaps:" if result["gaps"] else "\nNo gaps.")
        for g in result["gaps"]:
            print(f"  {g['shift']}: short {g['missing']} {g['role']}")
        if result["warnings"]:
            print("\nStaffing warnings:")
            for w in result["warnings"]:
                print("  " + w["message"])
        print(f"\nSaved {result.get('xlsx_path')}")
