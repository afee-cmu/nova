"""
Nova fire-time calculator
=========================
Given a menu (item -> station + cook time), how many portions each station can cook at
once, and the pending orders, decide WHEN to fire each item so that:
  - orders are ready by their pickup time,
  - items in the same order finish together (long items fire first, short ones later),
  - no station is ever asked to cook more than it physically can.

Rush slowdowns come from capacity, not slower cooking: a burger always takes ~6 min,
but during a rush it waits for a free spot on the grill. This models exactly that.

It plans ALL pending orders together with Google OR-Tools (CP-SAT), so firing one
order's fries "just in time" never blocks another order's 12-minute wings.
Pure code, no LLM. Entry points:
  plan_fires(menu, stations, orders, now)               -> fire queue, order status, station load
  quote_new_order(menu, stations, orders, now, items)   -> honest prep time to quote a new order

---------------------------------------------------------------------------
DATA MODEL
---------------------------------------------------------------------------
menu:      {"Smash Burger": {"station": "grill", "cook_min": 6},
            "Double Smash": {"station": "grill", "cook_min": 7, "space": 2}, ...}
stations:  {"grill": 6, "fryer": 4, "cold": 1}   # spots each station has
           # "space" (default 1) = spots one portion takes up. Grill of 6 spots fits
           # 6 Smash Burgers, or 3 Double Smashes, or any mix (2 doubles + 2 singles).
orders:    [{"id": "DD-1042", "channel": "DoorDash", "placed": "18:02",
             "pickup": "18:25",                   # "HH:MM", or "ASAP"
             "items": [{"name": "Smash Burger", "qty": 2}]}]
Times are "HH:MM" (24-hour), same day. Cook times may be fractional (e.g. 3.5).

Priorities (via penalty weights): no order late > keep the latest order as close to
on time as possible > items in an order finish together (food doesn't sit) > finish
right at pickup rather than far too early.
"""

from collections import defaultdict

from ortools.sat.python import cp_model

UNITS_PER_MIN = 2      # solver time unit = 30 seconds
HOLD_BUFFER_MIN = 1    # aim to finish this many minutes before pickup

W_LATE = 1000          # per unit an order is late
W_MAX_LATE = 300       # per unit of the single latest order (spreads delay fairly)
W_HOLD = 20            # per unit a finished item sits waiting for the rest of its order
W_EARLY = 2            # per unit an order is ready earlier than needed


def _t(hhmm):
    h, m = hhmm.strip().split(":")
    return int(h) * 60 + int(m)


def _fmt(minutes):
    minutes = round(minutes)
    return f"{(minutes // 60) % 24:02d}:{minutes % 60:02d}"


def _u(minutes):
    return int(round(minutes * UNITS_PER_MIN))


# ---------------------------------------------------------------------------
# Validation (catches LLM or typing mistakes before planning)
# ---------------------------------------------------------------------------
def validate_kitchen(menu, stations, orders=()):
    errors = []
    for st, slots in stations.items():
        if not isinstance(slots, int) or slots < 1:
            errors.append(f"Station '{st}': capacity must be a whole number of at least 1")
    for name, mi in menu.items():
        if mi.get("station") not in stations:
            errors.append(f"Menu item '{name}': unknown station '{mi.get('station')}'")
        if not isinstance(mi.get("cook_min"), (int, float)) or mi["cook_min"] <= 0:
            errors.append(f"Menu item '{name}': cook_min must be a positive number")
        space = mi.get("space", 1)
        if not isinstance(space, int) or space < 1:
            errors.append(f"Menu item '{name}': space must be a whole number of at least 1")
        elif isinstance(stations.get(mi.get("station")), int) and space > stations[mi["station"]]:
            errors.append(f"Menu item '{name}': takes {space} spots but {mi['station']} only has {stations[mi['station']]}")
    seen = set()
    for o in orders:
        oid = o.get("id", "<no id>")
        if oid in seen:
            errors.append(f"Order {oid}: duplicate id")
        seen.add(oid)
        try:
            _t(o.get("placed", "00:00"))
        except Exception:
            errors.append(f"Order {oid}: 'placed' must be HH:MM")
        if o.get("pickup") not in (None, "ASAP"):
            try:
                _t(o["pickup"])
            except Exception:
                errors.append(f"Order {oid}: 'pickup' must be HH:MM or ASAP")
        if not o.get("items"):
            errors.append(f"Order {oid}: has no items")
        for it in o.get("items", []):
            if it.get("name") not in menu:
                errors.append(f"Order {oid}: '{it.get('name')}' is not on the menu")
            if not isinstance(it.get("qty", 1), int) or it.get("qty", 1) < 1:
                errors.append(f"Order {oid}: qty for '{it.get('name')}' must be a whole number of at least 1")
    return errors


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
def _target_u(order, menu, now_u):
    """Time (in units) the order should be ready. ASAP = as soon as its slowest item could be."""
    if order.get("pickup") in (None, "ASAP"):
        return now_u + max(_u(menu[it["name"]]["cook_min"]) for it in order["items"])
    return max(now_u, _u(_t(order["pickup"]) - HOLD_BUFFER_MIN))


def _solve(menu, stations, orders, now, late_caps=None, focus=None, time_limit_s=2):
    """Build and solve. late_caps: {order_id: max lateness in units} (used for quoting).
    focus: an order id whose ready time is minimized first (used for quoting)."""
    now_u = _u(_t(now))
    total = sum(_u(menu[it["name"]]["cook_min"]) * it.get("qty", 1) for o in orders for it in o["items"])
    horizon = now_u + total + max([_target_u(o, menu, now_u) - now_u for o in orders] + [0]) + 1

    m = cp_model.CpModel()
    by_station = defaultdict(list)     # station -> intervals
    space_used = defaultdict(list)     # station -> spots each interval takes
    units, ready, late = {}, {}, {}
    objective = []

    for o in orders:
        oid, ends = o["id"], []
        units[oid] = []
        for it in o["items"]:
            dur = _u(menu[it["name"]]["cook_min"])
            for k in range(it.get("qty", 1)):
                s = m.NewIntVar(now_u, horizon, f"s|{oid}|{it['name']}|{k}")
                e = m.NewIntVar(now_u, horizon, f"e|{oid}|{it['name']}|{k}")
                st = menu[it["name"]]["station"]
                by_station[st].append(m.NewIntervalVar(s, dur, e, f"i|{oid}|{it['name']}|{k}"))
                space_used[st].append(menu[it["name"]].get("space", 1))
                units[oid].append((it["name"], s, e))
                ends.append(e)

        target = _target_u(o, menu, now_u)
        ready[oid] = m.NewIntVar(now_u, horizon, f"ready|{oid}")
        m.AddMaxEquality(ready[oid], ends)
        late[oid] = m.NewIntVar(0, horizon, f"late|{oid}")
        m.Add(late[oid] >= ready[oid] - target)
        early = m.NewIntVar(0, horizon, f"early|{oid}")
        m.Add(early >= target - ready[oid])
        if late_caps and oid in late_caps:
            m.Add(late[oid] <= late_caps[oid])

        objective += [W_LATE * late[oid], W_EARLY * early,
                      W_HOLD * (len(ends) * ready[oid] - sum(ends))]   # minutes food sits

    for st, intervals in by_station.items():
        m.AddCumulative(intervals, space_used[st], stations[st])

    max_late = m.NewIntVar(0, horizon, "max_late")
    m.AddMaxEquality(max_late, list(late.values()))
    objective.append(W_MAX_LATE * max_late)
    if focus:
        objective.append(W_LATE * 10 * ready[focus])

    m.Minimize(sum(objective))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = time_limit_s
    status = solver.Solve(m)
    if status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return None
    return solver, units, ready, late, now_u


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def plan_fires(menu, stations, orders, now):
    """Plan every pending order at time `now`. Re-run whenever an order arrives or is done."""
    solved = _solve(menu, stations, orders, now)
    if solved is None:
        return {"status": "no_solution"}
    solver, units, ready, late, now_u = solved
    U = UNITS_PER_MIN

    fires, results, station_use = [], [], defaultdict(list)
    for o in orders:
        oid = o["id"]
        r = solver.Value(ready[oid]) / U
        if o.get("pickup") in (None, "ASAP"):
            status, late_by = f"ready {_fmt(r)} ({round(r - now_u / U)} min)", 0
        else:
            slack = _t(o["pickup"]) - r
            late_by = max(0, -slack)
            status = (f"on time, ready {round(slack)} min before pickup" if slack >= 0
                      else f"LATE by {round(-slack)} min: update the quoted time on {o.get('channel', 'the app')}")
        results.append({"id": oid, "channel": o.get("channel", ""), "pickup": o.get("pickup", "ASAP"),
                        "ready": _fmt(r), "late_by_min": round(late_by), "status": status})

        grouped = defaultdict(int)
        for name, s, e in units[oid]:
            fs, fe = solver.Value(s) / U, solver.Value(e) / U
            grouped[(fs, name, fe)] += 1
            station_use[menu[name]["station"]].append((fs, fe, menu[name].get("space", 1)))
        for (fs, name, fe), qty in grouped.items():
            fires.append({"fire_at": _fmt(fs), "_t": fs, "qty": qty, "item": name,
                          "station": menu[name]["station"], "done_at": _fmt(fe),
                          "order": oid, "channel": o.get("channel", ""), "order_ready": _fmt(r)})

    fires.sort(key=lambda f: (f["_t"], f["order"]))
    for f in fires:
        f.pop("_t")
    results.sort(key=lambda r: r["ready"])

    now_m = now_u / U
    load = {}
    for st, cap in stations.items():
        intervals = station_use.get(st, [])
        in_use = sum(sp for fs, fe, sp in intervals if fs <= now_m < fe)
        busy_until = max([fe for _, fe, _ in intervals], default=now_m)
        load[st] = {"in_use_now": in_use, "capacity": cap, "busy_until": _fmt(busy_until),
                    "message": (f"{in_use}/{cap} spots in use now, booked until {_fmt(busy_until)}"
                                if intervals else f"0/{cap} in use, nothing booked")}

    return {"status": "ok", "now": now, "fire_queue": fires, "orders": results, "stations": load}


def quote_new_order(menu, stations, orders, now, items):
    """Minutes to quote for a new order, without making any existing order later than planned."""
    base = _solve(menu, stations, orders, now)
    if base is None:
        return None
    solver, _, _, late, _ = base
    caps = {oid: solver.Value(v) for oid, v in late.items()}
    new = {"id": "__NEW__", "pickup": "ASAP", "items": items}
    solved = _solve(menu, stations, orders + [new], now, late_caps=caps, focus="__NEW__")
    if solved is None:
        return None
    solver, _, ready, _, now_u = solved
    return round((solver.Value(ready["__NEW__"]) - now_u) / UNITS_PER_MIN)


# ---------------------------------------------------------------------------
# Demo: a Friday dinner rush
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    menu = {
        "Smash Burger":     {"station": "grill", "cook_min": 6},
        "Double Smash":     {"station": "grill", "cook_min": 7, "space": 2},
        "Chicken Sandwich": {"station": "fryer", "cook_min": 8},
        "Wings (10pc)":     {"station": "fryer", "cook_min": 12},
        "Fries":            {"station": "fryer", "cook_min": 4},
        "Onion Rings":      {"station": "fryer", "cook_min": 5},
        "Caesar Salad":     {"station": "cold",  "cook_min": 3},
        "Milkshake":        {"station": "cold",  "cook_min": 3},
    }
    stations = {"grill": 6, "fryer": 4, "cold": 1}
    orders = [
        {"id": "DD-1042", "channel": "DoorDash",  "placed": "18:01", "pickup": "18:20",
         "items": [{"name": "Wings (10pc)", "qty": 2}, {"name": "Fries", "qty": 2}]},
        {"id": "UE-88",   "channel": "Uber Eats", "placed": "18:02", "pickup": "18:15",
         "items": [{"name": "Smash Burger", "qty": 3}, {"name": "Fries", "qty": 3}, {"name": "Milkshake", "qty": 2}]},
        {"id": "PH-1",    "channel": "Phone",     "placed": "18:03", "pickup": "18:30",
         "items": [{"name": "Double Smash", "qty": 2}, {"name": "Onion Rings", "qty": 1}, {"name": "Caesar Salad", "qty": 1}]},
        {"id": "GH-311",  "channel": "Grubhub",   "placed": "18:04", "pickup": "18:16",
         "items": [{"name": "Chicken Sandwich", "qty": 2}, {"name": "Wings (10pc)", "qty": 1}, {"name": "Fries", "qty": 2}]},
        {"id": "WALK-7",  "channel": "In-store",  "placed": "18:05", "pickup": "ASAP",
         "items": [{"name": "Smash Burger", "qty": 1}, {"name": "Fries", "qty": 1}]},
    ]

    errors = validate_kitchen(menu, stations, orders)
    if errors:
        raise SystemExit("\n".join(errors))

    plan = plan_fires(menu, stations, orders, now="18:05")

    print(f"FIRE QUEUE (now {plan['now']})")
    for f in plan["fire_queue"]:
        print(f"  {f['fire_at']}  FIRE {f['qty']}x {f['item']:<17} [{f['station']}]  "
              f"for {f['order']} ({f['channel']}), order ready {f['order_ready']}")

    print("\nORDERS")
    for o in plan["orders"]:
        print(f"  {o['id']:<8} {o['channel']:<9} pickup {o['pickup']:<5} ready {o['ready']}  {o['status']}")

    print("\nSTATIONS")
    for s, b in plan["stations"].items():
        print(f"  {s:<6} {b['message']}")

    q = quote_new_order(menu, stations, orders, "18:05",
                        [{"name": "Wings (10pc)", "qty": 1}, {"name": "Fries", "qty": 1}])
    print(f"\nNew DoorDash order for wings + fries right now: quote {q} min")
