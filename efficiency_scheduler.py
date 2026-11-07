"""
Efficiency-first scheduler.

Where scheduler.py minimizes labor COST, this maximizes EFFICIENCY: it places each
person where they are rated most efficient, and weights busy windows more heavily so the
strongest people land in the rushes and wait times drop. Pay is deliberately NOT part of
the objective. It still obeys the same hard limits — coverage, one role per shift, no
over-staffing, and max hours/shifts (overtime is never scheduled automatically) — and
flags anyone left under their minimum hours.

Front-of-house / back-of-house: each person has a domain (front/back/any) and each role
has one too; a person is only ever assigned roles in their domain, so FOH stays FOH and
BOH stays BOH. This is a hard constraint.

This module is pure optimization: no network/LLM calls, no environment variables, and it
never touches an API key. (Per the design, the API — read via os.getenv("api_key")
elsewhere — is only for turning an employer's raw metrics into ratings and explaining
results, not for computing the schedule.)

    from schedule_input import load_workbook_input, efficiency_inputs
    from efficiency_scheduler import solve_efficient_schedule, format_report
    data, issues = load_workbook_input("filled.xlsx")
    emps, config, extras = efficiency_inputs(data)
    result = solve_efficient_schedule(emps, config, **extras)
    print(format_report(result))
"""
from collections import defaultdict

from ortools.sat.python import cp_model

from scheduler import (
    DAY_ORDER, SHORTAGE_PENALTY_CENTS, MIN_HOURS_PENALTY_CENTS_PER_MIN,
    build_shifts, employee_summary,
)
from reoptimize import rights_flags

# Scale turns a rating (e.g. 1-5) times a window weight into an integer objective term.
EFFICIENCY_SCALE = 100


def _domains_compatible(emp_domain, role_domain):
    """A person may work a role only within their front/back domain ('any' is a wildcard)."""
    if emp_domain in (None, "any") or role_domain in (None, "any"):
        return True
    return emp_domain == role_domain


def solve_efficient_schedule(employees, config, efficiency=None, role_domain=None,
                             window_weight=None, time_limit_s=10):
    """Maximize total weighted efficiency subject to coverage, caps, and FOH/BOH.

    employees : scheduler-format profiles; each may carry "domain" and "min_hours".
    efficiency: {(employee_id, role): rating}  higher = more efficient (default 1.0).
    role_domain: {role: "front"|"back"|"any"}  (role = the scheduler role / lowercased task).
    window_weight: {shift_name: weight}  busy windows weigh more (default 1).
    """
    efficiency = efficiency or {}
    role_domain = role_domain or {}
    window_weight = window_weight or {}

    emp = {e["id"]: e for e in employees}
    shifts, demand = build_shifts(config["rush_profile"])
    roles_needed = defaultdict(set)
    for d in demand:
        roles_needed[d["shift"]].add(d["role"])

    m = cp_model.CpModel()
    x, eff_term = {}, {}
    skipped_cross_domain = 0
    for eid, e in emp.items():
        available = set(e.get("available", []))
        e_domain = e.get("domain", "any")
        for s, info in shifts.items():
            if s not in available:
                continue
            for r in e.get("roles", {}):
                if r not in roles_needed[s]:
                    continue
                if not _domains_compatible(e_domain, role_domain.get(r)):
                    skipped_cross_domain += 1
                    continue   # hard FOH/BOH separation
                x[eid, s, r] = m.NewBoolVar(f"{eid}|{s}|{r}")
                rating = float(efficiency.get((eid, r), 1.0))
                weight = float(window_weight.get(s, 1))
                eff_term[eid, s, r] = round(rating * weight * EFFICIENCY_SCALE)

    by_emp_shift = defaultdict(list)
    by_emp = defaultdict(list)
    by_shift_role = defaultdict(list)
    for (eid, s, r), v in x.items():
        by_emp_shift[eid, s].append(v)
        by_emp[eid].append((s, v))
        by_shift_role[s, r].append(v)

    for vs in by_emp_shift.values():
        m.Add(sum(vs) <= 1)                       # one role per person per shift

    for eid, items in by_emp.items():
        e = emp[eid]
        m.Add(sum(shifts[s]["minutes"] * v for s, v in items) <= int(e["max_hours"] * 60))
        if e.get("max_shifts") is not None:
            m.Add(sum(v for _, v in items) <= int(e["max_shifts"]))

    short = {}
    for d in demand:
        key = (d["shift"], d["role"])
        staffed = sum(by_shift_role.get(key, []))
        short[key] = m.NewIntVar(0, d["needed"], f"short|{d['shift']}|{d['role']}")
        m.Add(staffed + short[key] >= d["needed"])
        m.Add(staffed <= d["needed"])             # never over-staff

    under_min = {}
    for eid, e in emp.items():
        items = by_emp.get(eid, [])
        min_minutes = int((e.get("min_hours") or 0) * 60)
        if min_minutes > 0 and items:
            under_min[eid] = m.NewIntVar(0, min_minutes, f"under|{eid}")
            m.Add(sum(shifts[s]["minutes"] * v for s, v in items) + under_min[eid] >= min_minutes)

    # Cover demand first (huge), then meet minimums, then MAXIMIZE efficiency (negative =
    # reward). Pay never enters the objective.
    m.Minimize(
        SHORTAGE_PENALTY_CENTS * sum(short.values())
        + MIN_HOURS_PENALTY_CENTS_PER_MIN * sum(under_min.values())
        - sum(eff_term[k] * v for k, v in x.items())
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "no_solution", "skipped_cross_domain": skipped_cross_domain}

    assignments = []
    for (eid, s, r), v in x.items():
        if solver.Value(v):
            info = shifts[s]
            assignments.append({
                "employee_id": eid, "employee_name": emp[eid]["name"],
                "shift": s, "day": info["day"], "window": info["name"],
                "start": info["start"], "end": info["end"], "role": r,
                "hours": info["minutes"] / 60,
                "cost": 0.0,   # pay is intentionally not part of the efficiency objective
                "efficiency": float(efficiency.get((eid, r), 1.0)),
                "window_weight": float(window_weight.get(s, 1)),
            })
    assignments.sort(key=lambda a: (DAY_ORDER.index(a["day"]), a["start"], a["role"], a["employee_name"]))

    gaps = [
        {"shift": s, "day": shifts[s]["day"], "window": shifts[s]["name"],
         "role": r, "missing": solver.Value(v)}
        for (s, r), v in short.items() if solver.Value(v) > 0
    ]
    gaps.sort(key=lambda g: (DAY_ORDER.index(g["day"]), shifts[g["shift"]]["start"], g["role"]))

    summary = employee_summary(assignments, employees)
    result = {
        "status": "optimal" if status == cp_model.OPTIMAL else "feasible",
        "assignments": assignments,
        "gaps": gaps,
        "employees": summary,
        "total_efficiency": round(sum(a["efficiency"] * a["window_weight"] for a in assignments), 2),
        "skipped_cross_domain": skipped_cross_domain,
    }
    result["flags"] = rights_flags(employees, result, shifts)
    return result


def format_report(result):
    if result["status"] == "no_solution":
        return "No feasible schedule found."
    lines = [f"Status: {result['status']}   Weighted efficiency score: {result['total_efficiency']}"]
    for a in result["assignments"]:
        busy = "  <= busy" if a["window_weight"] > 1 else ""
        lines.append(f"  {a['day']} {a['window']:<7} {a['start']}-{a['end']}  "
                     f"{a['employee_name']:<7} {a['role']:<10} (eff {a['efficiency']:g}){busy}")
    if result["gaps"]:
        lines.append("Gaps:")
        for g in result["gaps"]:
            lines.append(f"  ! {g['day']} {g['window']} short {g['missing']} {g['role']}")
    else:
        lines.append("No gaps — all demand covered.")
    if result["flags"]:
        lines.append("Worker-rights flags (your decision):")
        for f in result["flags"]:
            lines.append(f"  [{f['severity']}] {f['message']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    config = {
        "default_role_rates": {"grill": 17, "expo": 15, "cashier": 14},
        "rush_profile": {"Fri": [
            {"name": "lunch",  "window": "11:00-14:00", "staff": {"grill": 1, "cashier": 1}},
            {"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1, "cashier": 1}},
        ]},
    }
    employees = [
        {"id": "e1", "name": "Alex",  "roles": {"grill": None}, "max_hours": 40, "domain": "back",
         "available": ["Fri_lunch", "Fri_dinner"]},
        {"id": "e2", "name": "Ben",   "roles": {"grill": None}, "max_hours": 4, "domain": "back",
         "available": ["Fri_lunch", "Fri_dinner"]},
        {"id": "e3", "name": "Cara",  "roles": {"cashier": None}, "max_hours": 40, "domain": "front",
         "available": ["Fri_lunch", "Fri_dinner"]},
    ]
    efficiency = {("e1", "grill"): 5, ("e2", "grill"): 2, ("e3", "cashier"): 4}
    role_domain = {"grill": "back", "cashier": "front"}
    window_weight = {"Fri_dinner": 3, "Fri_lunch": 1}   # dinner is the rush

    res = solve_efficient_schedule(employees, config, efficiency, role_domain, window_weight)
    print(format_report(res))
