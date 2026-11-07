"""
Tests for efficiency_scheduler.py and schedule_input.efficiency_inputs.

Run:  python3 -m unittest test_efficiency -v
"""
import os
import tempfile
import unittest

import openpyxl

from efficiency_scheduler import solve_efficient_schedule
from schedule_input import load_workbook_input, efficiency_inputs, _domain


def cfg(rush):
    return {"default_role_rates": {"grill": 17, "cashier": 14, "expo": 15}, "rush_profile": rush}


def triples(result):
    return {(a["employee_id"], a["shift"], a["role"]) for a in result["assignments"]}


class TestEfficiencyObjective(unittest.TestCase):
    def test_more_efficient_worker_is_chosen(self):
        config = cfg({"Fri": [{"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1}}]})
        emps = [
            {"id": "a", "name": "A", "roles": {"grill": None}, "max_hours": 40, "domain": "back",
             "available": ["Fri_dinner"]},
            {"id": "b", "name": "B", "roles": {"grill": None}, "max_hours": 40, "domain": "back",
             "available": ["Fri_dinner"]},
        ]
        r = solve_efficient_schedule(emps, config, efficiency={("a", "grill"): 5, ("b", "grill"): 2},
                                     role_domain={"grill": "back"})
        self.assertIn(("a", "Fri_dinner", "grill"), triples(r))
        self.assertNotIn(("b", "Fri_dinner", "grill"), triples(r))

    def test_pay_is_ignored(self):
        # wages aren't even an input here; the efficient worker wins regardless of "pay"
        config = cfg({"Fri": [{"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1}}]})
        emps = [
            {"id": "x", "name": "X", "roles": {"grill": 99}, "max_hours": 40, "domain": "back",
             "available": ["Fri_dinner"]},   # expensive but efficient
            {"id": "y", "name": "Y", "roles": {"grill": 1}, "max_hours": 40, "domain": "back",
             "available": ["Fri_dinner"]},    # cheap but weak
        ]
        r = solve_efficient_schedule(emps, config, efficiency={("x", "grill"): 5, ("y", "grill"): 1},
                                     role_domain={"grill": "back"})
        self.assertIn(("x", "Fri_dinner", "grill"), triples(r))

    def test_no_over_staffing(self):
        config = cfg({"Fri": [{"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1}}]})
        emps = [
            {"id": "a", "name": "A", "roles": {"grill": None}, "max_hours": 40, "domain": "back",
             "available": ["Fri_dinner"]},
            {"id": "b", "name": "B", "roles": {"grill": None}, "max_hours": 40, "domain": "back",
             "available": ["Fri_dinner"]},
        ]
        r = solve_efficient_schedule(emps, config, efficiency={("a", "grill"): 5, ("b", "grill"): 5},
                                     role_domain={"grill": "back"})
        self.assertEqual(len(r["assignments"]), 1)


class TestBusyWindowWeighting(unittest.TestCase):
    def test_best_worker_goes_to_the_busy_window(self):
        config = cfg({"Fri": [
            {"name": "lunch",  "window": "11:00-14:00", "staff": {"grill": 1}},   # 3h
            {"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1}},   # 4h
        ]})
        # each cook caps at 4h, so neither can take both shifts -> one each
        emps = [
            {"id": "a", "name": "A", "roles": {"grill": None}, "max_hours": 4, "domain": "back",
             "available": ["Fri_lunch", "Fri_dinner"]},
            {"id": "b", "name": "B", "roles": {"grill": None}, "max_hours": 4, "domain": "back",
             "available": ["Fri_lunch", "Fri_dinner"]},
        ]
        r = solve_efficient_schedule(
            emps, config, efficiency={("a", "grill"): 5, ("b", "grill"): 3},
            role_domain={"grill": "back"}, window_weight={"Fri_lunch": 1, "Fri_dinner": 3})
        self.assertIn(("a", "Fri_dinner", "grill"), triples(r))   # strongest in the rush
        self.assertIn(("b", "Fri_lunch", "grill"), triples(r))


class TestFrontBackSeparation(unittest.TestCase):
    def test_front_person_never_assigned_back_role(self):
        config = cfg({"Fri": [{"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1}}]})
        # a front-of-house person is (wrongly) rated for grill; must NOT be placed there
        emps = [{"id": "f", "name": "Front", "roles": {"grill": None}, "max_hours": 40,
                 "domain": "front", "available": ["Fri_dinner"]}]
        r = solve_efficient_schedule(emps, config, efficiency={("f", "grill"): 5},
                                     role_domain={"grill": "back"})
        self.assertEqual(r["assignments"], [])
        self.assertGreater(r["skipped_cross_domain"], 0)
        self.assertTrue(any(g["role"] == "grill" for g in r["gaps"]))

    def test_back_person_fills_back_role(self):
        config = cfg({"Fri": [{"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1}}]})
        emps = [{"id": "c", "name": "Cook", "roles": {"grill": None}, "max_hours": 40,
                 "domain": "back", "available": ["Fri_dinner"]}]
        r = solve_efficient_schedule(emps, config, efficiency={("c", "grill"): 5},
                                     role_domain={"grill": "back"})
        self.assertIn(("c", "Fri_dinner", "grill"), triples(r))


class TestLimitsStillHold(unittest.TestCase):
    def test_overtime_not_scheduled_but_flagged(self):
        # one cook, cap 4h, two shifts (3h + 4h) can't both fit -> a gap + overtime flag
        config = cfg({"Fri": [
            {"name": "lunch",  "window": "11:00-14:00", "staff": {"grill": 1}},
            {"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1}},
        ]})
        emps = [{"id": "a", "name": "A", "roles": {"grill": None}, "max_hours": 4, "domain": "back",
                 "available": ["Fri_lunch", "Fri_dinner"]}]
        r = solve_efficient_schedule(emps, config, efficiency={("a", "grill"): 5},
                                     role_domain={"grill": "back"})
        a = next(e for e in r["employees"] if e["employee_id"] == "a")
        self.assertLessEqual(a["hours"], 4)
        self.assertEqual(len(r["gaps"]), 1)
        self.assertTrue(any(f["kind"] == "overtime_to_cover" for f in r["flags"]))

    def test_under_min_hours_flagged(self):
        config = cfg({"Fri": [{"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1}}]})
        emps = [{"id": "a", "name": "A", "roles": {"grill": None}, "max_hours": 40, "min_hours": 20,
                 "domain": "back", "available": ["Fri_dinner"]}]
        r = solve_efficient_schedule(emps, config, efficiency={("a", "grill"): 5},
                                     role_domain={"grill": "back"})
        self.assertTrue(any(f["kind"] == "under_min_hours" and f["who"] == "A" for f in r["flags"]))


class TestEfficiencyInputsBuilder(unittest.TestCase):
    """Build efficiency inputs from a filled workbook."""
    def _filled(self):
        wb = openpyxl.load_workbook(os.path.join(os.path.dirname(__file__),
                                                 "schedule_input_template.xlsx"))
        wb["Employees"].append(["E010", "Alex", "Cook", "", "Full-time", 10, 40, 6, 40])
        wb["Employees"].append(["E011", "Sam", "Server", "", "Part-time", 5, 25, 6, 40])
        wb["Availability"].append(["E010", "Fri", "16:00", "23:00", "Preferred"])
        wb["Availability"].append(["E011", "Fri", "16:00", "23:00", "Preferred"])
        wb["Proficiency"].append(["E010", "Fry Station", 5, None, 160])   # back
        wb["Proficiency"].append(["E011", "Floor", 4, None, None])        # front
        wb["RushWindows"].append(["Fri", "Dinner", "18:00", "22:00", "Fry Station", 1, 3])  # priority 3
        wb["RushWindows"].append(["Fri", "Dinner", "18:00", "22:00", "Floor", 1, 3])
        p = os.path.join(tempfile.mkdtemp(), "filled.xlsx")
        wb.save(p)
        return p

    def test_builds_efficiency_domains_and_weights(self):
        data, issues = load_workbook_input(self._filled())
        self.assertFalse(issues.has_errors(), issues.errors)
        emps, config, extras = efficiency_inputs(data)

        # efficiency ratings keyed by (employee, lowercased task), from proficiency scores
        self.assertEqual(extras["efficiency"][("E010", "fry station")], 5.0)
        self.assertEqual(extras["efficiency"][("E011", "floor")], 4.0)
        # domains: Fry Station is a Cook task (back), Floor is a Server task (front)
        self.assertEqual(extras["role_domain"]["fry station"], "back")
        self.assertEqual(extras["role_domain"]["floor"], "front")
        # employee domains from their role
        alex = next(e for e in emps if e["id"] == "E010")
        sam = next(e for e in emps if e["id"] == "E011")
        self.assertEqual(alex["domain"], "back")
        self.assertEqual(sam["domain"], "front")
        # busy-window weight picked up from priority column
        self.assertEqual(extras["window_weight"]["Fri_Dinner"], 3)

    def test_end_to_end_solves(self):
        data, _ = load_workbook_input(self._filled())
        emps, config, extras = efficiency_inputs(data)
        r = solve_efficient_schedule(emps, config, **extras)
        self.assertIn(r["status"], ("optimal", "feasible"))
        self.assertEqual(r["gaps"], [])
        # each person stays in their domain
        self.assertIn(("E010", "Fri_Dinner", "fry station"), triples(r))
        self.assertIn(("E011", "Fri_Dinner", "floor"), triples(r))


class TestDomainMap(unittest.TestCase):
    def test_known_domains(self):
        self.assertEqual(_domain("Cook"), "back")
        self.assertEqual(_domain("Dishwasher"), "back")
        self.assertEqual(_domain("Server"), "front")
        self.assertEqual(_domain("Cashier"), "front")
        self.assertEqual(_domain("Manager"), "any")
        self.assertEqual(_domain("Something New"), "any")   # unknown -> no restriction


if __name__ == "__main__":
    unittest.main(verbosity=2)
