"""Build and exercise the BonApp demo workbook from the canonical template."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path
from time import perf_counter

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = ROOT / "schedule_input_template.xlsx"
OUTPUT_PATH = Path(__file__).resolve().parent / "demo_input.xlsx"
sys.path.insert(0, str(ROOT))

TASKS = [
    ("Fry Station", "Cook", "Fryers — fries, wings, etc.", 16.25, 120),
    ("Grill", "Cook", "Grill / flat-top line", 19.00, 90),
    ("Expo", "Cook", "Plating and order assembly", 17.50, None),
    ("Register", "Cashier", "Front register / drive-thru", 15.50, 60),
    ("Floor", "Server", "Serving tables on the floor", 16.00, None),
]

# Only Maria and Leo are trained on Grill, creating a clear single-point-of-failure risk.
EMPLOYEES = [
    {"id": "E101", "name": "Maria Alvarez", "primary": "Cook", "secondary": "", "type": "Full-time", "min": 0, "max": 20, "shifts": 5, "ot": 20, "skills": ["Grill", "Fry Station"]},
    {"id": "E102", "name": "Leo Martinez", "primary": "Cook", "secondary": "Cashier", "type": "Full-time", "min": 0, "max": 20, "shifts": 5, "ot": 20, "skills": ["Grill", "Register"]},
    {"id": "E103", "name": "Jake Thompson", "primary": "Cook", "secondary": "", "type": "Part-time", "min": 0, "max": 24, "shifts": 6, "ot": 24, "skills": ["Fry Station", "Expo"]},
    {"id": "E104", "name": "Lily Chen", "primary": "Cook", "secondary": "Cashier", "type": "Part-time", "min": 0, "max": 24, "shifts": 6, "ot": 24, "skills": ["Fry Station", "Expo", "Register"]},
    {"id": "E105", "name": "Sam Rivera", "primary": "Server", "secondary": "Cook, Cashier", "type": "Full-time", "min": 0, "max": 30, "shifts": 6, "ot": 30, "skills": ["Expo", "Floor", "Register"]},
    {"id": "E106", "name": "Aisha Patel", "primary": "Server", "secondary": "Cashier", "type": "Part-time", "min": 0, "max": 24, "shifts": 6, "ot": 24, "skills": ["Floor", "Register"]},
    {"id": "E107", "name": "Noah Brooks", "primary": "Cashier", "secondary": "Server", "type": "Part-time", "min": 0, "max": 26, "shifts": 6, "ot": 26, "skills": ["Register", "Floor"]},
    {"id": "E108", "name": "Grace Kim", "primary": "Server", "secondary": "Cook", "type": "Part-time", "min": 0, "max": 24, "shifts": 6, "ot": 24, "skills": ["Floor", "Fry Station"]},
    {"id": "E109", "name": "Priya Shah", "primary": "Cook", "secondary": "Server", "type": "Part-time", "min": 0, "max": 30, "shifts": 6, "ot": 30, "skills": ["Fry Station", "Expo", "Floor"]},
    {"id": "E110", "name": "Ethan Miller", "primary": "Cook", "secondary": "Cashier", "type": "Part-time", "min": 0, "max": 32, "shifts": 6, "ot": 32, "skills": ["Fry Station", "Register", "Expo"]},
]

DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WINDOWS = {
    "Lunch Rush": ("11:00", "14:00"),
    "Dinner Rush": ("17:00", "21:00"),
}


def clear_data_rows(sheet):
    """Clear old example/data values while retaining the template's formatting."""
    for row in sheet.iter_rows(min_row=2, max_row=sheet.max_row):
        for cell in row:
            cell.value = None


def put_rows(sheet, rows):
    original_max_row = sheet.max_row
    for row_number, values in enumerate(rows, start=2):
        for column, value in enumerate(values, start=1):
            cell = sheet.cell(row=row_number, column=column, value=value)
            if row_number > original_max_row:
                template_cell = sheet.cell(row=2, column=column)
                if template_cell.has_style:
                    cell._style = template_cell._style


def is_available(employee, day, window_label):
    if employee["id"] == "E101" and day == "Sat" and window_label == "Dinner Rush":
        return False
    if day == "Fri" and window_label == "Dinner Rush" and employee["id"] in {"E103", "E104", "E109", "E110"}:
        return False
    return True


def build_workbook():
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Canonical workbook template not found: {TEMPLATE_PATH}")
    shutil.copy2(TEMPLATE_PATH, OUTPUT_PATH)
    workbook = load_workbook(OUTPUT_PATH)

    task_sheet = workbook["Tasks"]
    clear_data_rows(task_sheet)
    put_rows(task_sheet, TASKS)

    employee_rows = [
        (person["id"], person["name"], person["primary"], person["secondary"],
         person["type"], person["min"], person["max"], person["shifts"], person["ot"])
        for person in EMPLOYEES
    ]
    employee_sheet = workbook["Employees"]
    clear_data_rows(employee_sheet)
    put_rows(employee_sheet, employee_rows)

    availability_rows = []
    for person in EMPLOYEES:
        for day in DAYS:
            lunch = is_available(person, day, "Lunch Rush")
            dinner = is_available(person, day, "Dinner Rush")
            if lunch and dinner:
                availability_rows.append((person["id"], day, "10:00", "22:00", "Available"))
            elif lunch:
                availability_rows.append((person["id"], day, "10:00", "15:00", "Available"))
    availability_sheet = workbook["Availability"]
    clear_data_rows(availability_sheet)
    put_rows(availability_sheet, availability_rows)

    default_rates = {task[0]: task[3] for task in TASKS}
    proficiency_rows = []
    for index, person in enumerate(EMPLOYEES):
        for skill_index, task in enumerate(person["skills"]):
            # Individual rates vary while remaining close to task defaults.
            delta = ((index + skill_index) % 5 - 2) * 0.5
            rate = round(default_rates[task] + delta, 2)
            score = 3 + ((index + skill_index) % 3)
            throughput = {"Fry Station": 120, "Grill": 90, "Expo": None,
                          "Register": 60, "Floor": None}[task]
            proficiency_rows.append((person["id"], task, score, rate, throughput))
    proficiency_sheet = workbook["Proficiency"]
    clear_data_rows(proficiency_sheet)
    put_rows(proficiency_sheet, proficiency_rows)

    demand_rows = []
    for day in DAYS:
        for task in ("Fry Station", "Register"):
            demand_rows.append((day, "Lunch Rush", *WINDOWS["Lunch Rush"], task, 1))
        dinner_demand = [("Grill", 1), ("Expo", 1), ("Floor", 1),
                         ("Fry Station", 1), ("Register", 1)]
        if day == "Fri":
            dinner_demand = [(task, 2 if task == "Expo" else count) for task, count in dinner_demand]
        if day == "Sat":
            dinner_demand = [(task, 2 if task == "Grill" else count) for task, count in dinner_demand]
        for task, count in dinner_demand:
            demand_rows.append((day, "Dinner Rush", *WINDOWS["Dinner Rush"], task, count))
    rush_sheet = workbook["RushWindows"]
    clear_data_rows(rush_sheet)
    put_rows(rush_sheet, demand_rows)

    policy_sheet = workbook["Policy"]
    policy_values = {
        "overtime_allowed": "No",
        "max_overtime_hours_per_employee_week": 0,
        "overtime_requires_approval": "Yes",
        "scheduling_objective": "Cover rush windows while respecting weekly hour caps",
    }
    for row in range(2, policy_sheet.max_row + 1):
        setting = policy_sheet.cell(row=row, column=1).value
        if setting in policy_values:
            policy_sheet.cell(row=row, column=2, value=policy_values[setting])

    workbook.save(OUTPUT_PATH)
    return OUTPUT_PATH


def run_and_report(path):
    from schedule_input import load_workbook_input, to_scheduler_inputs

    data, issues = load_workbook_input(path)
    print("Validation errors:", issues.errors)
    print("Validation warnings:", issues.warnings)
    if issues.has_errors():
        return 1

    employees, config = to_scheduler_inputs(data)
    from scheduler import run_pipeline

    started = perf_counter()
    result = run_pipeline(employees, config)
    elapsed = perf_counter() - started
    print("Solver status:", result.get("status"))
    print(f"Solve time: {elapsed:.3f}s")
    for gap in result.get("gaps", []):
        print(f"Gap: {gap['shift']} | {gap['role']} | missing {gap['missing']}")
    if not result.get("gaps"):
        print("Gaps: none")
    hours_by_employee = {row["employee_id"]: row for row in result.get("employees", [])}
    for employee in employees:
        hours = hours_by_employee.get(employee["id"], {}).get("hours", 0)
        print(f"Hours: {employee['name']}: {hours:g}/{employee['max_hours']:g}")

    if result.get("status") in {"optimal", "feasible"}:
        if len(result.get("gaps", [])) not in {2, 3}:
            print("Demo target not met: expected 2-3 gaps.")
            return 2
        over_cap = [row for row in result.get("employees", [])
                    if row["hours"] > next(e["max_hours"] for e in employees if e["id"] == row["employee_id"])]
        if over_cap:
            print("Demo target not met: an employee exceeds their max hours.")
            return 2
        if elapsed >= 5:
            print("Demo target not met: solve should finish under 5 seconds.")
            return 2
    return 0 if result.get("status") in {"optimal", "feasible"} else 2


if __name__ == "__main__":
    workbook_path = build_workbook()
    print(f"Created demo workbook: {workbook_path}")
    try:
        sys.exit(run_and_report(workbook_path))
    except ModuleNotFoundError as error:
        if error.name == "ortools":
            print("Solver status: unavailable (install ortools to run the solver).")
            print("The workbook was created; validation was completed before importing scheduler.")
            sys.exit(3)
        raise
