"""
Tests for reoptimize.py — minimal-disruption re-scheduling plus worker-rights flags.

Run:  python3 -m unittest test_reoptimize -v
"""
import unittest

from scheduler import run_pipeline
from reoptimize import reoptimize, schedule_diff


def cfg(rush):
    # everyone_gets_a_shift off so baselines are deterministically cost-minimal;
    # that keeps the idle backfill person (Taylor) out of the base schedule.
    return {"default_role_rates": {"grill": 17, "cashier": 14, "expo": 15},
            "everyone_gets_a_shift": False,
            "rush_profile": rush}


# Mon-only so build_shifts doesn't expand to the whole week.
MON = {"Mon": [
    {"name": "lunch", "window": "11:00-14:00", "staff": {"grill": 1, "cashier": 1}},
    {"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1, "cashier": 1, "expo": 1}},
]}

STAFF = [
    {"id": "e1", "name": "Alex", "roles": {"grill": None}, "max_hours": 40, "min_hours": 6,
     "available": ["Mon_lunch", "Mon_dinner"]},
    {"id": "e2", "name": "Sam", "roles": {"cashier": None}, "max_hours": 40,
     "available": ["Mon_lunch", "Mon_dinner"]},
    {"id": "e3", "name": "Priya", "roles": {"expo": None}, "max_hours": 40,
     "available": ["Mon_dinner"]},
    {"id": "e4", "name": "Taylor", "roles": {"grill": 20}, "max_hours": 40,   # pricier backfill
     "available": ["Mon_dinner"]},
]


def triples(assignments):
    return {(a["employee_id"], a["shift"], a["role"]) for a in assignments}


class TestMinimalDisruption(unittest.TestCase):
    def setUp(self):
        self.config = cfg(MON)
        self.base = run_pipeline(STAFF, self.config)
        self.assertIn(self.base["status"], ("optimal", "feasible"))
        # baseline should put Alex (cheaper) on the dinner grill
        self.assertIn(("e1", "Mon_dinner", "grill"), triples(self.base["assignments"]))

    def test_cancel_backfills_one_slot_only(self):
        r = reoptimize(STAFF, self.config, self.base["assignments"],
                       cancel=[{"employee_id": "e1", "shift": "Mon_dinner"}])
        self.assertEqual(r["status"], "optimal")
        self.assertEqual(r["gaps"], [])
        # exactly one out, one in; everything else untouched
        self.assertEqual(len(r["diff"]["removed"]), 1)
        self.assertEqual(len(r["diff"]["added"]), 1)
        self.assertEqual(r["diff"]["removed"][0]["employee_id"], "e1")
        self.assertEqual(r["diff"]["added"][0]["employee_id"], "e4")  # Taylor
        self.assertEqual(len(r["diff"]["kept"]), 4)

    def test_unrelated_assignments_are_stable(self):
        r = reoptimize(STAFF, self.config, self.base["assignments"],
                       cancel=[{"employee_id": "e1", "shift": "Mon_dinner"}])
        kept = {(k["employee_id"], k["shift"], k["role"]) for k in r["diff"]["kept"]}
        self.assertIn(("e2", "Mon_lunch", "cashier"), kept)
        self.assertIn(("e3", "Mon_dinner", "expo"), kept)
        self.assertIn(("e1", "Mon_lunch", "grill"), kept)  # Alex keeps his lunch

    def test_under_min_hours_flag(self):
        r = reoptimize(STAFF, self.config, self.base["assignments"],
                       cancel=[{"employee_id": "e1", "shift": "Mon_dinner"}])
        kinds = {(f["kind"], f.get("who")) for f in r["flags"]}
        self.assertIn(("under_min_hours", "Alex"), kinds)   # 3h < 6h min


class TestNoSubstitute(unittest.TestCase):
    def test_gap_and_flag_when_nobody_can_cover(self):
        staff = [e for e in STAFF if e["id"] != "e4"]   # drop Taylor, the only backfill
        config = cfg(MON)
        base = run_pipeline(staff, config)
        r = reoptimize(staff, config, base["assignments"],
                       cancel=[{"employee_id": "e1", "shift": "Mon_dinner"}])
        # the dinner grill slot is now uncoverable
        self.assertTrue(any(g["shift"] == "Mon_dinner" and g["role"] == "grill" for g in r["gaps"]))
        # and the cancelled person is NOT suggested as a candidate
        covers = [f for f in r["flags"] if f["kind"] in ("no_available_staff", "no_trained_staff")]
        self.assertTrue(covers)
        self.assertFalse(any("Alex" in f["message"] for f in covers))


class TestOvertimeFlag(unittest.TestCase):
    def test_gap_needing_overtime_is_flagged_not_scheduled(self):
        # Alex is the only grill, cap 4h. Lunch 3h + dinner 4h = 7h can't both fit.
        config = cfg({"Mon": [
            {"name": "lunch", "window": "11:00-14:00", "staff": {"grill": 1}},
            {"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1}},
        ]})
        staff = [{"id": "e1", "name": "Alex", "roles": {"grill": None},
                  "max_hours": 4, "available": ["Mon_lunch", "Mon_dinner"]}]
        base = run_pipeline(staff, config)
        r = reoptimize(staff, config, base["assignments"])
        # one shift covered, one gap; Alex never exceeds his 4h cap
        alex = next(e for e in r["employees"] if e["employee_id"] == "e1")
        self.assertLessEqual(alex["hours"], 4)
        self.assertEqual(len(r["gaps"]), 1)
        self.assertTrue(any(f["kind"] == "overtime_to_cover" and "Alex" in f["message"]
                            for f in r["flags"]))


class TestEmergencyDemand(unittest.TestCase):
    def test_extra_demand_pulls_in_more_staff(self):
        config = cfg({"Mon": [{"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1}}]})
        staff = [
            {"id": "e1", "name": "Alex", "roles": {"grill": None}, "max_hours": 40,
             "available": ["Mon_dinner"]},
            {"id": "e4", "name": "Taylor", "roles": {"grill": 20}, "max_hours": 40,
             "available": ["Mon_dinner"]},
        ]
        base = run_pipeline(staff, config)
        self.assertEqual(len(base["assignments"]), 1)   # only 1 grill needed
        r = reoptimize(staff, config, base["assignments"],
                       add_demand=[{"shift": "Mon_dinner", "role": "grill", "needed": 1}])
        self.assertEqual(r["gaps"], [])
        self.assertEqual(len(r["assignments"]), 2)       # now both work
        self.assertEqual(len(r["diff"]["added"]), 1)

    def test_unknown_shift_in_emergency_is_reported(self):
        config = cfg({"Mon": [{"name": "dinner", "window": "17:00-21:00", "staff": {"grill": 1}}]})
        staff = [{"id": "e1", "name": "Alex", "roles": {"grill": None}, "max_hours": 40,
                  "available": ["Mon_dinner"]}]
        base = run_pipeline(staff, config)
        r = reoptimize(staff, config, base["assignments"],
                       add_demand=[{"shift": "Sat_brunch", "role": "grill", "needed": 1}])
        self.assertTrue(any("Sat_brunch" in p for p in r["problems"]))


class TestPersonOutEntirely(unittest.TestCase):
    def test_whole_week_cancellation(self):
        config = cfg(MON)
        base = run_pipeline(STAFF, config)
        r = reoptimize(STAFF, config, base["assignments"],
                       cancel=[{"employee_id": "e1"}])   # Alex out entirely, no shift given
        # Alex appears in nothing
        self.assertFalse(any(a["employee_id"] == "e1" for a in r["assignments"]))
        # his dinner grill is backfilled by Taylor; lunch grill now a gap (no other lunch grill)
        self.assertIn(("e4", "Mon_dinner", "grill"), triples(r["assignments"]))
        self.assertTrue(any(g["shift"] == "Mon_lunch" and g["role"] == "grill" for g in r["gaps"]))


class TestScheduleDiffUnit(unittest.TestCase):
    def test_diff_classification(self):
        old = [{"employee_id": "e1", "employee_name": "Alex", "shift": "Mon_dinner", "role": "grill"},
               {"employee_id": "e2", "employee_name": "Sam", "shift": "Mon_dinner", "role": "cashier"}]
        new = [{"employee_id": "e2", "employee_name": "Sam", "shift": "Mon_dinner", "role": "cashier"},
               {"employee_id": "e4", "employee_name": "Taylor", "shift": "Mon_dinner", "role": "grill"}]
        d = schedule_diff(old, new)
        self.assertEqual([x["employee_id"] for x in d["kept"]], ["e2"])
        self.assertEqual([x["employee_id"] for x in d["removed"]], ["e1"])
        self.assertEqual([x["employee_id"] for x in d["added"]], ["e4"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
