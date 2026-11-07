"""
Regression tests for schedule_input: load_workbook_input + to_scheduler_inputs.

Builds workbooks in a temp dir from scratch and asserts on the result. Also checks
the adapter output feeds scheduler.py (end-to-end test skipped if ortools absent).

Run:  python3 -m unittest test_schedule_input -v
"""
import copy
import os
import tempfile
import unittest
from datetime import time

import openpyxl

from schedule_input import (
    load_workbook_input, to_scheduler_inputs,
    _matches_example, _parse_time, Issues,
    EMP_HEADERS, AV_HEADERS, PF_HEADERS, RW_HEADERS,
    _EX_EMPLOYEE, _EX_RUSH,
)

TASK_HEADERS = ["task", "role", "description", "default_hourly_rate", "target_units_per_hour"]

# ---- a minimal VALID, schedulable workbook ----
BASE = {
    "Roles": [
        ["role", "description"],
        ["Cook", "bo"], ["Server", "fo"], ["Cashier", "reg"],
        ["Dishwasher", "dish"], ["Host", "host"], ["Manager", "mgr"],
    ],
    "Tasks": [
        TASK_HEADERS,
        ["Fry Station", "Cook", "fryers", 16.0, 120],
        ["Register", "Cashier", "reg", 14.0, 60],
        ["Floor", "Server", "tables", 13.0, None],
        ["Dish Pit", "Dishwasher", "dish", 13.0, None],
    ],
    "Employees": [
        EMP_HEADERS,
        ["E010", "Alex", "Cook", "", "Full-time", 10, 40, 5, 40],
        ["E011", "Sam", "Cashier", "Server", "Part-time", 5, 25, 5, 40],
    ],
    "Availability": [
        AV_HEADERS,
        ["E010", "Mon", "09:00", "17:00", "Preferred"],
        ["E011", "Fri", "16:00", "22:00", "Available"],
    ],
    "Proficiency": [
        PF_HEADERS,
        ["E010", "Fry Station", 5, None, 160],
        ["E011", "Register", 5, None, 70],
    ],
    "RushWindows": [
        RW_HEADERS,
        ["Mon", "Lunch", "11:30", "14:00", "Fry Station", 1],
        ["Fri", "Dinner", "18:00", "21:00", "Register", 1],
    ],
    "Policy": [
        ["setting", "value", "description"],
        ["overtime_allowed", "No", "d"],
        ["max_overtime_hours_per_employee_week", 0, "d"],
        ["overtime_requires_approval", "Yes", "d"],
        ["scheduling_objective", "Minimize labor cost, cover every rush", "d"],
    ],
}


def write_wb(sheets, path):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    wb.save(path)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def load(self, sheets):
        p = os.path.join(self.dir, "wb.xlsx")
        write_wb(sheets, p)
        return load_workbook_input(p)

    def mutate(self, **sheet_rows):
        s = copy.deepcopy(BASE)
        s.update(sheet_rows)
        return s


class TestValid(Base):
    def test_base_is_valid(self):
        data, issues = self.load(BASE)
        self.assertFalse(issues.has_errors(), issues.errors)
        self.assertFalse(issues.warnings, issues.warnings)
        self.assertEqual(len(data["employees"]), 2)
        self.assertEqual(len(data["tasks"]), 4)
        self.assertEqual(len(data["demand"]), 2)

    def test_task_skills_nested(self):
        data, _ = self.load(BASE)
        alex = next(e for e in data["employees"] if e["employee_id"] == "E010")
        self.assertIn("Fry Station", alex["task_skills"])
        self.assertEqual(alex["task_skills"]["Fry Station"]["proficiency"], 5.0)

    def test_demand_role_derived_from_task(self):
        data, _ = self.load(BASE)
        reg = next(d for d in data["demand"] if d["task"] == "Register")
        self.assertEqual(reg["role"], "Cashier")


class TestStructure(Base):
    def test_missing_sheet(self):
        s = copy.deepcopy(BASE)
        del s["Proficiency"]
        data, issues = self.load(s)
        self.assertIsNone(data)
        self.assertTrue(any("Proficiency" in e for e in issues.errors))

    def test_empty_tasks(self):
        _, issues = self.load(self.mutate(Tasks=[TASK_HEADERS]))
        self.assertTrue(any("Tasks tab is empty" in e for e in issues.errors))

    def test_no_employees(self):
        _, issues = self.load(self.mutate(Employees=[EMP_HEADERS]))
        self.assertTrue(any("No employees" in e for e in issues.errors))


class TestTasks(Base):
    def test_task_unknown_role(self):
        _, issues = self.load(self.mutate(Tasks=[
            TASK_HEADERS, ["Smoker", "Pitmaster", "bbq", 20, None]]))
        self.assertTrue(any("role 'Pitmaster'" in e for e in issues.errors))

    def test_task_bad_rate(self):
        _, issues = self.load(self.mutate(Tasks=[
            TASK_HEADERS, ["Fry Station", "Cook", "f", 0, None]]))
        self.assertTrue(any("greater than 0" in e for e in issues.errors))

    def test_demanded_task_without_rate_errors(self):
        # Floor demanded but its rate is blank
        s = self.mutate(
            Tasks=[TASK_HEADERS,
                   ["Fry Station", "Cook", "f", 16, None],
                   ["Floor", "Server", "t", None, None]],
            RushWindows=[RW_HEADERS, ["Mon", "Lunch", "11:30", "14:00", "Floor", 1]],
            Proficiency=[PF_HEADERS, ["E010", "Floor", 3, None, None]],
        )
        _, issues = self.load(s)
        self.assertTrue(any("no default_hourly_rate" in e for e in issues.errors))


class TestEmployees(Base):
    def test_duplicate_id(self):
        _, issues = self.load(self.mutate(Employees=[
            EMP_HEADERS,
            ["E010", "A", "Cook", "", "Full-time", 0, 40, 5, 40],
            ["E010", "B", "Cook", "", "Full-time", 0, 40, 5, 40],
        ]))
        self.assertTrue(any("duplicate employee_id" in e for e in issues.errors))

    def test_unknown_role(self):
        _, issues = self.load(self.mutate(Employees=[
            EMP_HEADERS, ["E010", "A", "Chef", "", "Full-time", 0, 40, 5, 40]]))
        self.assertTrue(any("role 'Chef'" in e for e in issues.errors))

    def test_min_exceeds_max(self):
        _, issues = self.load(self.mutate(Employees=[
            EMP_HEADERS, ["E010", "A", "Cook", "", "Full-time", 45, 40, 5, 40]]))
        self.assertTrue(any("exceeds max_hours_week" in e for e in issues.errors))

    def test_max_hours_required(self):
        _, issues = self.load(self.mutate(Employees=[
            EMP_HEADERS, ["E010", "A", "Cook", "", "Full-time", 0, None, 5, 40]]))
        self.assertTrue(any("max_hours_week is required" in e for e in issues.errors))

    def test_bad_employment_type_is_warning(self):
        data, issues = self.load(self.mutate(Employees=[
            EMP_HEADERS,
            ["E010", "Alex", "Cook", "", "Seasonal", 10, 40, 5, 40],
            ["E011", "Sam", "Cashier", "Server", "Part-time", 5, 25, 5, 40],
        ]))
        self.assertFalse(issues.has_errors(), issues.errors)
        self.assertTrue(any("employment_type" in w for w in issues.warnings))


class TestAvailability(Base):
    def test_orphan_employee(self):
        _, issues = self.load(self.mutate(Availability=[
            AV_HEADERS, ["E999", "Mon", "09:00", "17:00", "Preferred"]]))
        self.assertTrue(any("no matching Employees row" in e for e in issues.errors))

    def test_bad_day(self):
        _, issues = self.load(self.mutate(Availability=[
            AV_HEADERS, ["E010", "Funday", "09:00", "17:00", "Preferred"]]))
        self.assertTrue(any("day 'Funday'" in e for e in issues.errors))

    def test_start_after_end(self):
        _, issues = self.load(self.mutate(Availability=[
            AV_HEADERS, ["E010", "Mon", "17:00", "09:00", "Preferred"]]))
        self.assertTrue(any("before available_end" in e for e in issues.errors))

    def test_bad_time_format(self):
        _, issues = self.load(self.mutate(Availability=[
            AV_HEADERS, ["E010", "Mon", "9am", "5pm", "Preferred"]]))
        self.assertTrue(any("cannot read time" in e for e in issues.errors))

    def test_time_object_cells(self):
        _, issues = self.load(self.mutate(Availability=[
            AV_HEADERS, ["E010", "Mon", time(9, 0), time(17, 30), "Preferred"]]))
        self.assertFalse(issues.has_errors(), issues.errors)


class TestProficiency(Base):
    def test_unknown_task(self):
        _, issues = self.load(self.mutate(Proficiency=[
            PF_HEADERS, ["E010", "Sous Vide", 5, None, None]]))
        self.assertTrue(any("task 'Sous Vide'" in e for e in issues.errors))

    def test_score_out_of_range(self):
        _, issues = self.load(self.mutate(Proficiency=[
            PF_HEADERS, ["E010", "Fry Station", 6, None, None]]))
        self.assertTrue(any("between 1 and 5" in e for e in issues.errors))

    def test_score_required(self):
        _, issues = self.load(self.mutate(Proficiency=[
            PF_HEADERS, ["E010", "Fry Station", None, None, 160]]))
        self.assertTrue(any("'proficiency' is required" in e for e in issues.errors))

    def test_bad_wage(self):
        _, issues = self.load(self.mutate(Proficiency=[
            PF_HEADERS, ["E010", "Fry Station", 5, -3, None]]))
        self.assertTrue(any("hourly_rate must be greater than 0" in e for e in issues.errors))


class TestRushWindows(Base):
    def test_unknown_task(self):
        _, issues = self.load(self.mutate(RushWindows=[
            RW_HEADERS, ["Mon", "Lunch", "11:00", "14:00", "Teleport", 1]]))
        self.assertTrue(any("task 'Teleport'" in e for e in issues.errors))

    def test_blank_task_is_error(self):
        _, issues = self.load(self.mutate(RushWindows=[
            RW_HEADERS, ["Mon", "Lunch", "11:00", "14:00", "", 2]]))
        self.assertTrue(any("task is required" in e for e in issues.errors))

    def test_negative_staff(self):
        _, issues = self.load(self.mutate(RushWindows=[
            RW_HEADERS, ["Mon", "Lunch", "11:00", "14:00", "Fry Station", -2]]))
        self.assertTrue(any("cannot be negative" in e for e in issues.errors))

    def test_window_time_conflict(self):
        _, issues = self.load(self.mutate(RushWindows=[
            RW_HEADERS,
            ["Mon", "Lunch", "11:00", "14:00", "Fry Station", 1],
            ["Mon", "Lunch", "11:30", "14:00", "Register", 1],
        ]))
        self.assertTrue(any("conflicting times" in e for e in issues.errors))

    def test_untrained_station_warns(self):
        _, issues = self.load(self.mutate(RushWindows=[
            RW_HEADERS, ["Fri", "Dinner", "18:00", "21:00", "Dish Pit", 1]]))
        self.assertFalse(issues.has_errors(), issues.errors)
        self.assertTrue(any("Dish Pit" in w and "Proficiency" in w for w in issues.warnings))


class TestPolicyOvertime(Base):
    def test_overtime_off_by_default_value(self):
        data, _ = self.load(BASE)
        self.assertFalse(data["policy"]["overtime_allowed"])
        self.assertTrue(data["policy"]["overtime_requires_approval"])

    def test_overtime_garbage_defaults_false_and_warns(self):
        data, issues = self.load(self.mutate(Policy=[
            ["setting", "value", "description"], ["overtime_allowed", "Maybe", "d"]]))
        self.assertFalse(data["policy"]["overtime_allowed"])
        self.assertTrue(any("overtime_allowed" in w for w in issues.warnings))

    def test_overtime_threshold_is_reference_only(self):
        data, _ = self.load(BASE)
        alex = next(e for e in data["employees"] if e["employee_id"] == "E010")
        self.assertEqual(alex["overtime_threshold_hours"], 40.0)
        self.assertFalse(data["policy"]["overtime_allowed"])


class TestExampleSkipping(Base):
    def test_matches_example_exact(self):
        row = dict(zip(EMP_HEADERS, _EX_EMPLOYEE))
        self.assertTrue(_matches_example(row, EMP_HEADERS, _EX_EMPLOYEE))

    def test_edited_example_is_real_data(self):
        edited = list(_EX_EMPLOYEE)
        edited[1] = "Real Person"
        row = dict(zip(EMP_HEADERS, edited))
        self.assertFalse(_matches_example(row, EMP_HEADERS, _EX_EMPLOYEE))

    def test_shipped_template_blank_has_no_data(self):
        tmpl = os.path.join(os.path.dirname(__file__), "schedule_input_template.xlsx")
        if not os.path.exists(tmpl):
            self.skipTest("template not built")
        _, issues = load_workbook_input(tmpl)
        self.assertTrue(any("No employees" in e for e in issues.errors))

    def test_number_vs_string_match(self):
        row = dict(zip(RW_HEADERS, ("Fri", "Dinner Rush", "18:00", "21:00", "Floor", "4")))
        self.assertTrue(_matches_example(row, RW_HEADERS, _EX_RUSH))


# ---------------------------------------------------------------------------
# Adapter: normalized data -> scheduler inputs
# ---------------------------------------------------------------------------
class TestAdapter(Base):
    def setUp(self):
        super().setUp()
        self.data, issues = self.load(BASE)
        self.assertFalse(issues.has_errors(), issues.errors)
        self.emps, self.config = to_scheduler_inputs(self.data)

    def test_default_role_rates_from_tasks(self):
        self.assertEqual(self.config["default_role_rates"]["fry station"], 16.0)
        self.assertEqual(self.config["default_role_rates"]["register"], 14.0)

    def test_employee_roles_from_proficiency(self):
        alex = next(e for e in self.emps if e["id"] == "E010")
        self.assertEqual(alex["roles"], {"fry station": None})  # None -> use task default
        self.assertEqual(alex["max_hours"], 40)
        self.assertEqual(alex["max_shifts"], 5)

    def test_per_employee_wage_override(self):
        data, _ = self.load(self.mutate(Proficiency=[
            PF_HEADERS,
            ["E010", "Fry Station", 5, 21.5, 160],
            ["E011", "Register", 5, None, 70],
        ]))
        emps, _ = to_scheduler_inputs(data)
        alex = next(e for e in emps if e["id"] == "E010")
        self.assertEqual(alex["roles"]["fry station"], 21.5)

    def test_availability_coverage_to_shifts(self):
        alex = next(e for e in self.emps if e["id"] == "E010")   # Mon 09:00-17:00
        sam = next(e for e in self.emps if e["id"] == "E011")    # Fri 16:00-22:00
        self.assertIn("Mon_Lunch", alex["available"])            # 11:30-14:00 covered
        self.assertNotIn("Fri_Dinner", alex["available"])        # no Fri availability
        self.assertIn("Fri_Dinner", sam["available"])            # 18:00-21:00 covered

    def test_partial_coverage_excluded(self):
        # availability that does NOT fully cover the window -> not available
        data, _ = self.load(self.mutate(Availability=[
            AV_HEADERS,
            ["E010", "Mon", "12:00", "14:00", "Preferred"],   # starts after 11:30 window start
            ["E011", "Fri", "16:00", "22:00", "Available"],
        ]))
        emps, _ = to_scheduler_inputs(data)
        alex = next(e for e in emps if e["id"] == "E010")
        self.assertNotIn("Mon_Lunch", alex["available"])

    def test_rush_profile_shape(self):
        self.assertIn("Mon", self.config["rush_profile"])
        mon = self.config["rush_profile"]["Mon"][0]
        self.assertEqual(mon["name"], "Lunch")
        self.assertEqual(mon["window"], "11:30-14:00")
        self.assertEqual(mon["staff"], {"fry station": 1})

    def test_max_shifts_omitted_when_blank(self):
        data, _ = self.load(self.mutate(Employees=[
            EMP_HEADERS,
            ["E010", "Alex", "Cook", "", "Full-time", 10, 40, None, 40],
            ["E011", "Sam", "Cashier", "Server", "Part-time", 5, 25, 5, 40],
        ]))
        emps, _ = to_scheduler_inputs(data)
        alex = next(e for e in emps if e["id"] == "E010")
        self.assertNotIn("max_shifts", alex)


class TestEndToEndScheduler(Base):
    """Feed the adapter output straight into scheduler.run_pipeline."""
    def test_pipeline_produces_schedule(self):
        try:
            import scheduler
        except Exception as exc:   # ortools not installed
            self.skipTest(f"scheduler import failed: {exc}")
        data, issues = self.load(BASE)
        self.assertFalse(issues.has_errors(), issues.errors)
        emps, config = to_scheduler_inputs(data)

        # the adapter output must pass scheduler's own validation
        shifts, demand = scheduler.build_shifts(config["rush_profile"])
        errs = scheduler.validate_input(emps, config, shifts, demand)
        self.assertEqual(errs, [], errs)

        result = scheduler.run_pipeline(emps, config)
        self.assertIn(result["status"], ("optimal", "feasible"), result)
        self.assertEqual(result["gaps"], [], result["gaps"])
        roles_assigned = {(a["employee_id"], a["role"]) for a in result["assignments"]}
        self.assertIn(("E010", "fry station"), roles_assigned)
        self.assertIn(("E011", "register"), roles_assigned)


class TestParseTimeUnit(unittest.TestCase):
    def test_variants(self):
        iss = Issues()
        self.assertEqual(_parse_time("09:00", "x", iss), 540)
        self.assertEqual(_parse_time("17:30", "x", iss), 1050)
        self.assertEqual(_parse_time(time(6, 15), "x", iss), 375)
        self.assertIsNone(_parse_time(None, "x", iss))
        self.assertFalse(iss.errors)
        self.assertIsNone(_parse_time("noon", "x", iss))
        self.assertTrue(iss.errors)


if __name__ == "__main__":
    unittest.main(verbosity=2)
