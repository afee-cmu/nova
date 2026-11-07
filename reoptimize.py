"""
Fast schedule RE-optimization for cancellations and emergencies.

The base scheduler (scheduler.build_schedule) solves a week from scratch and will
happily reshuffle everyone. That is wrong for a live week: staff have already been
told their shifts. When someone calls out or an emergency hits, we want to change as
LITTLE as possible — cover the hole, leave everyone else alone — and we want it fast.

This module builds a second CP-SAT model from scheduler.py's own pieces
(build_shifts, resolve_wage, employee_summary, SHORTAGE_PENALTY_CENTS) that:
  * locks out the cancelled assignment(s),
  * rewards keeping every still-valid existing assignment (minimal disruption),
  * optionally adds emergency demand,
  * still covers as much demand as possible at the lowest cost,
and then reports a clear diff plus any WORKER-RIGHTS concerns for the manager.

Worker rights (surfaced, and minimized first):
  * max hours / max shifts are HARD caps — never exceeded automatically; if a gap can
    only be filled by exceeding them, that is flagged as needing overtime (your call),
    never done silently.
  * anyone pushed UNDER their minimum hours by the change is flagged.

NOTE: this is pure optimization. It makes no network/LLM calls, reads no environment
variables, and never touches an API key. (The only module that needs a key is
pipeline.py, via os.getenv("api_key"); this module does not import it.)

    from reoptimize import reoptimize, format_report
    result = reoptimize(employees, config, current_assignments,
                        cancel=[{"employee_id": "emp_001", "shift": "Fri_dinner"}])
    print(format_report(result))
"""
from collections import defaultdict

from ortools.sat.python import cp_model

from scheduler import (
    DAY_ORDER, SHORTAGE_PENALTY_CENTS, MIN_HOURS_PENALTY_CENTS_PER_MIN,
    build_shifts, resolve_wage, employee_summary,
)

# Reward (in cents) for keeping one existing assignment. Must sit between a single
# assignment's labor cost (so stability beats cheap reshuffling) and the shortage
# penalty (so covering a real gap always beats preserving one old assignment).
DEFAULT_STABILITY_CENTS = 100_000


def _triple(a):
    return (a["employee_id"], a["shift"], a["role"])


def _forbidden_pairs(cancel, shifts):
    """Expand a cancellation spec into the set of (employee_id, shift) to lock out.

    Each entry is one of:
      {"employee_id": X, "shift": S}  -> X can't work shift S
      {"employee_id": X, "day": D}    -> X can't work any shift on day D
      {"employee_id": X}              -> X is out entirely this week
    """
    forbidden = set()
    for c in cancel or []:
        eid = c.get("employee_id")
        if not eid:
            continue
        if c.get("shift"):
            forbidden.add((eid, c["shift"]))
        elif c.get("day"):
            for s, info in shifts.items():
                if info["day"] == c["day"]:
                    forbidden.add((eid, s))
        else:
            for s in shifts:
                forbidden.add((eid, s))
    return forbidden


def _merge_demand(demand, add_demand, shifts):
    """Combine base demand with emergency extra demand into {(shift, role): needed}."""
    merged = defaultdict(int)
    for d in demand:
        merged[(d["shift"], d["role"])] += int(d["needed"])
    problems = []
    for d in add_demand or []:
        shift, role, needed = d.get("shift"), d.get("role"), int(d.get("needed", 0))
        if shift not in shifts:
            problems.append(f"emergency demand for unknown shift '{shift}'")
            continue
        if needed > 0:
            merged[(shift, role)] += needed
    return dict(merged), problems


def reoptimize(employees, config, current_assignments,
               cancel=None, add_demand=None,
               stability_cents=DEFAULT_STABILITY_CENTS, time_limit_s=10):
    """Re-solve the week with minimal disruption after a cancellation / emergency.

    Returns a dict:
      status, assignments, gaps, labor_cost, employees (per-person summary),
      diff {kept, added, removed}, flags (worker-rights concerns), problems.
    """
    rates = config["default_role_rates"]
    emp = {e["id"]: e for e in employees}
    shifts, base_demand = build_shifts(config["rush_profile"])
    merged_demand, problems = _merge_demand(base_demand, add_demand, shifts)
    forbidden = _forbidden_pairs(cancel, shifts)
    roles_needed = defaultdict(set)   # shift -> roles that shift actually needs
    for (s, role) in merged_demand:
        roles_needed[s].add(role)

    m = cp_model.CpModel()
    x, cost_cents = {}, {}
    for eid, e in emp.items():
        available = set(e.get("available", []))
        for s, info in shifts.items():
            if s not in available or (eid, s) in forbidden:
                continue
            for r in e.get("roles", {}):
                if r in roles_needed[s]:
                    x[eid, s, r] = m.NewBoolVar(f"{eid}|{s}|{r}")
                    cost_cents[eid, s, r] = round(resolve_wage(e, r, rates) * info["minutes"] / 60 * 100)

    by_emp_shift = defaultdict(list)
    by_emp = defaultdict(list)
    by_shift_role = defaultdict(list)
    for (eid, s, r), v in x.items():
        by_emp_shift[eid, s].append(v)
        by_emp[eid].append((s, v))
        by_shift_role[s, r].append(v)

    for vs in by_emp_shift.values():
        m.Add(sum(vs) <= 1)
    for eid, items in by_emp.items():
        e = emp[eid]
        m.Add(sum(shifts[s]["minutes"] * v for s, v in items) <= int(e["max_hours"] * 60))
        if e.get("max_shifts") is not None:
            m.Add(sum(v for _, v in items) <= int(e["max_shifts"]))

    short = {}
    for (s, r), needed in merged_demand.items():
        staffed = sum(by_shift_role.get((s, r), []))
        short[s, r] = m.NewIntVar(0, needed, f"short|{s}|{r}")
        m.Add(staffed + short[s, r] >= needed)
        m.Add(staffed <= needed)           # never over-staff a role (matches scheduler.py)

    # Soft minimums: minutes each person falls below their min_hours. Kept BELOW the
    # stability reward, so re-optimization won't reshuffle the plan just to top up hours
    # (it flags an under-minimum instead); it only breaks ties in that direction.
    under_min = {}
    for eid, e in emp.items():
        items = by_emp.get(eid, [])
        min_minutes = int((e.get("min_hours") or 0) * 60)
        if min_minutes > 0 and items:
            under_min[eid] = m.NewIntVar(0, min_minutes, f"under|{eid}")
            m.Add(sum(shifts[s]["minutes"] * v for s, v in items) + under_min[eid] >= min_minutes)

    # existing assignments that are still possible -> reward keeping them
    keep_vars = [x[t] for t in (_triple(a) for a in current_assignments) if t in x]

    m.Minimize(
        SHORTAGE_PENALTY_CENTS * sum(short.values())
        - stability_cents * sum(keep_vars)
        + MIN_HOURS_PENALTY_CENTS_PER_MIN * sum(under_min.values())
        + sum(cost_cents[k] * v for k, v in x.items())
    )

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {"status": "no_solution", "problems": problems}

    assignments = []
    for (eid, s, r), v in x.items():
        if solver.Value(v):
            info = shifts[s]
            assignments.append({
                "employee_id": eid, "employee_name": emp[eid]["name"],
                "shift": s, "day": info["day"], "window": info["name"],
                "start": info["start"], "end": info["end"], "role": r,
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
    result = {
        "status": "optimal" if status == cp_model.OPTIMAL else "feasible",
        "assignments": assignments,
        "gaps": gaps,
        "labor_cost": round(sum(a["cost"] for a in assignments), 2),
        "employees": summary,
        "diff": schedule_diff(current_assignments, assignments),
        "problems": problems,
    }
    result["flags"] = rights_flags(employees, result, shifts, forbidden)
    return result


def schedule_diff(old, new):
    """What changed between two assignment lists, as (employee_id, shift, role) triples."""
    old_set = {_triple(a) for a in old}
    new_set = {_triple(a) for a in new}
    name = {a["employee_id"]: a.get("employee_name", a["employee_id"]) for a in list(old) + list(new)}

    def fmt(t):
        eid, s, r = t
        return {"employee_id": eid, "employee_name": name.get(eid, eid), "shift": s, "role": r}

    return {
        "kept": [fmt(t) for t in sorted(old_set & new_set)],
        "added": [fmt(t) for t in sorted(new_set - old_set)],
        "removed": [fmt(t) for t in sorted(old_set - new_set)],
    }


def rights_flags(employees, result, shifts, forbidden=None):
    """Worker-rights concerns for the manager. Each: {severity, kind, who?, message}."""
    forbidden = forbidden or set()
    emp = {e["id"]: e for e in employees}
    flags = []

    for row in result["employees"]:
        e = emp.get(row["employee_id"], {})
        name = row["name"]
        if row["max_hours"] is not None and row["hours"] > row["max_hours"]:
            flags.append({"severity": "high", "kind": "over_max_hours", "who": name,
                          "message": f"{name} is scheduled {row['hours']:g}h, over their "
                                     f"{row['max_hours']:g}h max — overtime pay likely applies."})
        if row.get("max_shifts") is not None and row["shifts"] > row["max_shifts"]:
            flags.append({"severity": "high", "kind": "over_max_shifts", "who": name,
                          "message": f"{name} has {row['shifts']} shifts, over their "
                                     f"{row['max_shifts']}-shift max."})
        min_h = e.get("min_hours")
        if min_h and row["hours"] < min_h:
            flags.append({"severity": "medium", "kind": "under_min_hours", "who": name,
                          "message": f"{name} is down to {row['hours']:g}h, under their "
                                     f"{min_h:g}h minimum — may breach a guaranteed-hours agreement."})

    # gaps: can they only be covered by overtime / over a cap?
    used = {row["employee_id"]: row for row in result["employees"]}
    working = defaultdict(set)
    for a in result["assignments"]:
        working[a["employee_id"]].add(a["shift"])

    for gap in result["gaps"]:
        s, r = gap["shift"], gap["role"]
        hrs = shifts[s]["minutes"] / 60
        overtime_options, need_training = [], True
        for e in employees:
            if r not in e.get("roles", {}):
                continue
            need_training = False  # someone is trained, just can't take it now
            if s in working[e["id"]] or (e["id"], s) in forbidden:
                continue  # already on it, or they are the one who cancelled it
            u = used.get(e["id"], {"hours": 0, "shifts": 0})
            over_hours = (u["hours"] + hrs) > e["max_hours"]
            over_shifts = (e.get("max_shifts") is not None and u["shifts"] + 1 > e["max_shifts"])
            if s not in e.get("available", []):
                continue  # not available -> not an overtime option, it's an availability ask
            if over_hours or over_shifts:
                why = "hours" if over_hours else "shift"
                overtime_options.append(f"{e['name']} (would exceed their {why} cap)")
        label = f"{gap['day']} {gap['window']} {r} (short {gap['missing']})"
        if overtime_options:
            flags.append({"severity": "high", "kind": "overtime_to_cover", "who": None,
                          "message": f"Gap {label}: coverable only with overtime — "
                                     + "; ".join(overtime_options) + ". Your call."})
        elif need_training:
            flags.append({"severity": "medium", "kind": "no_trained_staff", "who": None,
                          "message": f"Gap {label}: no one is trained for this role — "
                                     "consider cross-training, hiring, or lowering this window's staffing."})
        else:
            flags.append({"severity": "medium", "kind": "no_available_staff", "who": None,
                          "message": f"Gap {label}: trained staff exist but none are available — "
                                     "ask someone to extend availability, or lower staffing."})
    return flags


def format_report(result):
    """A compact, manager-readable summary of a re-optimization."""
    if result["status"] == "no_solution":
        return "No feasible schedule found."
    d = result["diff"]
    lines = [f"Status: {result['status']}   Labor cost: ${result['labor_cost']:.2f}",
             f"Changes: {len(d['added'])} added, {len(d['removed'])} removed, {len(d['kept'])} unchanged"]
    for a in d["removed"]:
        lines.append(f"  - REMOVED  {a['employee_name']:<8} {a['shift']} ({a['role']})")
    for a in d["added"]:
        lines.append(f"  + ADDED    {a['employee_name']:<8} {a['shift']} ({a['role']})")
    if result["gaps"]:
        lines.append("Gaps remaining:")
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
# Demo: build a baseline with scheduler, then react to a cancellation.
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from scheduler import run_pipeline

    config = {
        "default_role_rates": {"grill": 17, "cashier": 14, "expo": 15},
        "everyone_gets_a_shift": False,   # keep the baseline cost-minimal for a clear demo
        "rush_profile": {
            "Mon": [
                {"name": "lunch", "window": "11:00-14:00", "staff": {"grill": 1, "cashier": 1}},
                {"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1, "cashier": 1, "expo": 1}},
            ],
        },
    }
    employees = [
        {"id": "e1", "name": "Alex", "roles": {"grill": None}, "max_hours": 40, "min_hours": 6,
         "available": ["Mon_lunch", "Mon_dinner"]},
        {"id": "e2", "name": "Sam", "roles": {"cashier": None}, "max_hours": 40,
         "available": ["Mon_lunch", "Mon_dinner"]},
        {"id": "e3", "name": "Priya", "roles": {"expo": None}, "max_hours": 40,
         "available": ["Mon_dinner"]},
        # Taylor can also grill dinner, but costs a bit more -> baseline prefers Alex,
        # so Taylor is the natural backfill when Alex calls out.
        {"id": "e4", "name": "Taylor", "roles": {"grill": 20}, "max_hours": 40,
         "available": ["Mon_dinner"]},
    ]

    base = run_pipeline(employees, config)
    print("=== baseline ===")
    for a in base["assignments"]:
        print(f"  {a['shift']:<11} {a['employee_name']:<7} {a['role']}")

    print("\n=== Alex calls out for Mon_dinner ===")
    result = reoptimize(employees, config, base["assignments"],
                        cancel=[{"employee_id": "e1", "shift": "Mon_dinner"}])
    print(format_report(result))
