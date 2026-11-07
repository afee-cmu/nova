import unittest
from io import BytesIO
from openpyxl import Workbook

from menu_tickets import parse_menu_rows, parse_ticket_rows, estimate_backlog, read_menu_workbook
from employee_profiles import check_ratings, merge_approved_ratings


class ManagerFeatureTests(unittest.TestCase):
    def setUp(self):
        self.menu = {"Burger": {"station": "grill", "prep_minutes": 8},
                     "Fries": {"station": "fryer", "prep_minutes": 4}}

    def test_station_parallelism_and_completed_work(self):
        tickets = [{"ticket_id": "1", "item": "Burger", "quantity": 2, "status": "queued"},
                   {"ticket_id": "1", "item": "Fries", "quantity": 1, "status": "queued"},
                   {"ticket_id": "2", "item": "Burger", "quantity": 1, "status": "completed"}]
        estimate = estimate_backlog(self.menu, tickets, {"grill": 2, "fryer": 1})
        self.assertEqual(estimate["active_tickets"], 1)
        self.assertEqual(estimate["ticket_wait_minutes"]["1"], 8)
        self.assertEqual(estimate["estimated_clear_minutes"], 8)

    def test_malformed_menu_and_ticket_are_rejected(self):
        with self.assertRaises(ValueError):
            parse_menu_rows([["Burger", "grill", -1]])
        with self.assertRaises(ValueError):
            parse_menu_rows([["Burger", "grill", 1], ["burger", "grill", 3]])
        with self.assertRaises(ValueError):
            parse_ticket_rows([["99", "Unknown", 2, "queued"]], self.menu)
        with self.assertRaises(ValueError):
            parse_ticket_rows([["99", "Burger", 1.1, "queued"]], self.menu)
        with self.assertRaises(ValueError):
            estimate_backlog(self.menu, [{"ticket_id":"1","item":"Burger","quantity":1,"status":"queued"}], {"grill": 0})

    def test_workbook_and_untrusted_input(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "Menu"
        ws.append(["item", "station", "prep_minutes"])
        ws.append(["Burger", "grill", 8])
        ws = wb.create_sheet("Tickets")
        ws.append(["ticket_id", "item", "quantity", "status"])
        ws.append(["100", "Burger", 2, "queued"])
        buf = BytesIO()
        wb.save(buf)
        menu, tickets = read_menu_workbook(buf.getvalue())
        self.assertEqual(menu["Burger"]["prep_minutes"], 8)
        self.assertEqual(tickets[0]["quantity"], 2)
        with self.assertRaises(ValueError):
            read_menu_workbook(b"not a spreadsheet")

    def test_approval_only_for_known_tasks_and_employees(self):
        data = {"employees": [{"employee_id": "E001", "task_skills": {"Fry Station": {}, "Cashier": {}}}]}
        initial = {"efficiency": {("E001", "fry station"): 2.0}, "window_weight": {"Fri_dinner": 3}}
        merged = merge_approved_ratings(initial, {"E001": {"Fry Station": 10, "Cashier": 7}}, data)
        self.assertEqual(merged["efficiency"][("E001", "fry station")], 10)
        self.assertEqual(merged["window_weight"], initial["window_weight"])
        self.assertEqual(initial["efficiency"][("E001", "fry station")], 2.0)
        with self.assertRaises(ValueError):
            check_ratings({"Fry Station": 11}, ["Fry Station"])
        with self.assertRaises(ValueError):
            merge_approved_ratings(initial, {"E999": {"Fry Station": 5}}, data)


if __name__ == "__main__":
    unittest.main()
