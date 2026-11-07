"""Mock roster, schedule, and explanation for the UI skeleton."""

EMPLOYEES = [
    {"employee_id": "e1", "name": "Maria", "roles": ["grill", "prep"], "hourly_wage": 19.50, "max_weekly_hours": 18},
    {"employee_id": "e2", "name": "Devon", "roles": ["fry", "pack"], "hourly_wage": 17.00, "max_weekly_hours": 32},
    {"employee_id": "e3", "name": "Aisha", "roles": ["cashier", "pack"], "hourly_wage": 16.50, "max_weekly_hours": 30},
    {"employee_id": "e4", "name": "Leo", "roles": ["grill"], "hourly_wage": 20.00, "max_weekly_hours": 28},
    {"employee_id": "e5", "name": "Sofia", "roles": ["prep", "fry"], "hourly_wage": 18.00, "max_weekly_hours": 34},
    {"employee_id": "e6", "name": "Noah", "roles": ["cashier"], "hourly_wage": 16.00, "max_weekly_hours": 25},
    {"employee_id": "e7", "name": "Jamal", "roles": ["pack", "prep"], "hourly_wage": 17.50, "max_weekly_hours": 30},
    {"employee_id": "e8", "name": "Grace", "roles": ["grill", "fry"], "hourly_wage": 19.00, "max_weekly_hours": 35},
    {"employee_id": "e9", "name": "Ethan", "roles": ["cashier", "pack"], "hourly_wage": 16.75, "max_weekly_hours": 24},
    {"employee_id": "e10", "name": "Priya", "roles": ["prep", "fry"], "hourly_wage": 18.50, "max_weekly_hours": 32},
]

# Seven days, with lunch and dinner shifts on each day.
_ASSIGNMENTS = [
    ("e1", "Maria", "Mon", "lunch", "grill"), ("e3", "Aisha", "Mon", "lunch", "cashier"),
    ("e5", "Sofia", "Mon", "dinner", "prep"), ("e8", "Grace", "Mon", "dinner", "grill"),
    ("e2", "Devon", "Tue", "lunch", "fry"), ("e6", "Noah", "Tue", "lunch", "cashier"),
    ("e4", "Leo", "Tue", "dinner", "grill"), ("e7", "Jamal", "Tue", "dinner", "pack"),
    ("e10", "Priya", "Wed", "lunch", "prep"), ("e9", "Ethan", "Wed", "lunch", "cashier"),
    ("e2", "Devon", "Wed", "dinner", "fry"), ("e1", "Maria", "Wed", "dinner", "grill"),
    ("e5", "Sofia", "Thu", "lunch", "fry"), ("e3", "Aisha", "Thu", "lunch", "cashier"),
    ("e8", "Grace", "Thu", "dinner", "grill"), ("e10", "Priya", "Thu", "dinner", "prep"),
    ("e4", "Leo", "Fri", "lunch", "grill"), ("e6", "Noah", "Fri", "lunch", "cashier"),
    ("e1", "Maria", "Fri", "dinner", "grill"), ("e2", "Devon", "Fri", "dinner", "fry"),
    ("e3", "Aisha", "Fri", "dinner", "pack"),
    ("e5", "Sofia", "Sat", "lunch", "prep"), ("e9", "Ethan", "Sat", "lunch", "cashier"),
    ("e8", "Grace", "Sat", "dinner", "grill"), ("e7", "Jamal", "Sat", "dinner", "pack"),
    ("e10", "Priya", "Sun", "lunch", "prep"), ("e3", "Aisha", "Sun", "lunch", "cashier"),
    ("e4", "Leo", "Sun", "dinner", "grill"), ("e2", "Devon", "Sun", "dinner", "fry"),
]

ASSIGNMENTS = []
for index, (employee_id, name, day, shift, role) in enumerate(_ASSIGNMENTS):
    start, end = ("11:00", "16:00") if shift == "lunch" else ("17:00", "22:00")
    ASSIGNMENTS.append({
        "employee_id": employee_id, "name": name, "day": day, "shift": shift,
        "role": role, "start": start, "end": end, "hours": 5,
    })

GAPS = [
    {"gap_id": "g1", "day": "Sat", "shift": "dinner", "role": "grill", "needed": 2, "assigned": 1, "short_by": 1},
    {"gap_id": "g2", "day": "Fri", "shift": "dinner", "role": "pack", "needed": 2, "assigned": 1, "short_by": 1},
    {"gap_id": "g3", "day": "Sun", "shift": "lunch", "role": "fry", "needed": 1, "assigned": 0, "short_by": 1},
]

_WAGES = {employee["employee_id"]: employee["hourly_wage"] for employee in EMPLOYEES}
_TOTAL_COST = round(sum(item["hours"] * _WAGES[item["employee_id"]] for item in ASSIGNMENTS), 2)
_NEEDED_SLOTS = len(ASSIGNMENTS) + sum(gap["short_by"] for gap in GAPS)
_COVERAGE_RATE = len(ASSIGNMENTS) / _NEEDED_SLOTS

SCHEDULE = {
    "status": "OPTIMAL",
    "assignments": ASSIGNMENTS,
    "gaps": GAPS,
    "total_cost": _TOTAL_COST,
    "coverage_rate": _COVERAGE_RATE,
}

EXPLANATION = {
    "summary": "The weekly schedule covers most planned demand at an estimated labor cost for the scheduled hours. Three rush-period roles remain uncovered and may need a voluntary shift pickup or a manager-approved adjustment.",
    "gap_explanations": [
        {"gap_id": "g1", "explanation": "Saturday dinner is a high-volume period and needs one more grill-trained employee.", "suggested_action": "Ask an available grill-trained employee about picking up the shift, or reduce grill demand if the rush forecast allows.", "needs_manager_approval": True},
        {"gap_id": "g2", "explanation": "Friday dinner has one fewer packer than the forecasted order volume requires.", "suggested_action": "Check whether a cross-trained prep employee can cover packing during the peak window.", "needs_manager_approval": True},
        {"gap_id": "g3", "explanation": "Sunday lunch has no fry station coverage in the current roster assignment.", "suggested_action": "Offer a short lunch shift to an available fry-trained employee.", "needs_manager_approval": True},
    ],
}
