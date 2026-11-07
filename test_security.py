"""
Security regression tests (all local — no network, no live attacks).

These encode the defenses from the security review so they can't silently regress:
  * no hardcoded secrets / dangerous sinks in the tree
  * the API key is only ever read via os.getenv("api_key")
  * secrets and PII are git-ignored
  * untrusted .xlsx uploads are pre-flighted (zip-bomb / oversize guards)
  * spreadsheet formula injection is neutralized on export
  * files holding PII are written owner-only (0600)

Run:  python3 -m unittest test_security -v
"""
import os
import stat
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
PY_FILES = sorted(p for p in REPO.glob("*.py") if p.name != "test_security.py")


class TestNoHardcodedSecrets(unittest.TestCase):
    def test_no_secret_literals(self):
        import re
        bad = []
        # provider key prefixes + literal assignments to api_key/token/secret
        patterns = [
            re.compile(r"sk-[A-Za-z0-9]{12,}"),
            re.compile(r"sk-or-[A-Za-z0-9]{8,}"),
            re.compile(r"(?i)(api_key|secret|token|password)\s*=\s*['\"][^'\"]{6,}['\"]"),
            re.compile(r"Bearer\s+[A-Za-z0-9._-]{12,}"),   # a literal bearer token, not f-string
        ]
        for f in PY_FILES:
            for n, line in enumerate(f.read_text().splitlines(), 1):
                for p in patterns:
                    if p.search(line):
                        bad.append(f"{f.name}:{n}: {line.strip()}")
        self.assertEqual(bad, [], "possible hardcoded secret:\n" + "\n".join(bad))

    def test_key_only_from_env(self):
        # any file using an Authorization header must source the key from os.getenv("api_key")
        for f in PY_FILES:
            text = f.read_text()
            if "Authorization" in text:
                self.assertIn('os.getenv("api_key")', text,
                              f"{f.name} sends Authorization but doesn't read the key from the env var")


class TestNoDangerousSinks(unittest.TestCase):
    def test_no_dangerous_calls(self):
        import re
        banned = re.compile(r"\b(eval|exec)\s*\(|\bos\.system\b|subprocess|\bpickle\b|"
                            r"yaml\.load\b|shell\s*=\s*True|verify\s*=\s*False|__import__\s*\(")
        hits = []
        for f in PY_FILES:
            for n, line in enumerate(f.read_text().splitlines(), 1):
                if banned.search(line):
                    hits.append(f"{f.name}:{n}: {line.strip()}")
        self.assertEqual(hits, [], "dangerous sink:\n" + "\n".join(hits))


class TestGitignoreProtectsSecretsAndPII(unittest.TestCase):
    def test_gitignore_entries(self):
        gi = (REPO / ".gitignore")
        self.assertTrue(gi.exists(), ".gitignore missing")
        text = gi.read_text()
        for pat in (".env", "schedule.xlsx", "extracted_input.json"):
            self.assertIn(pat, text, f".gitignore should ignore {pat}")


class TestUploadPreflight(unittest.TestCase):
    def setUp(self):
        from schedule_input import preflight_upload
        self.preflight = preflight_upload
        self.dir = tempfile.mkdtemp()

    def test_real_template_is_allowed(self):
        tmpl = REPO / "schedule_input_template.xlsx"
        if not tmpl.exists():
            self.skipTest("template not built")
        self.assertIsNone(self.preflight(str(tmpl)))

    def test_non_zip_rejected(self):
        p = os.path.join(self.dir, "fake.xlsx")
        Path(p).write_text("this is not a zip")
        self.assertIsNotNone(self.preflight(p))

    def test_zip_bomb_ratio_rejected(self):
        # 2 MB of zeros compresses to a few KB -> huge ratio; caught before openpyxl loads
        p = os.path.join(self.dir, "bomb.xlsx")
        with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("xl/worksheets/sheet1.xml", b"0" * (2 * 1024 * 1024))
        msg = self.preflight(p)
        self.assertIsNotNone(msg)
        self.assertIn("zip bomb", msg)

    def test_oversize_rejected(self):
        import schedule_input
        p = os.path.join(self.dir, "big.xlsx")
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("x", b"data")
        orig = schedule_input.MAX_UPLOAD_BYTES
        try:
            schedule_input.MAX_UPLOAD_BYTES = 10   # force the size guard
            msg = schedule_input.preflight_upload(p)
        finally:
            schedule_input.MAX_UPLOAD_BYTES = orig
        self.assertIn("limit is", msg)


class TestExcelInjectionSanitizer(unittest.TestCase):
    def test_unit(self):
        from scheduler import _excel_safe
        for danger in ("=1+1", "+1", "-1", "@SUM(A1)", "\tx", "\rx"):
            self.assertTrue(_excel_safe(danger).startswith("'"), danger)
        for ok in ("Alex", "grill", "17:00", "dinner"):
            self.assertEqual(_excel_safe(ok), ok)

    def test_export_neutralizes_malicious_name(self):
        import openpyxl
        from scheduler import export_schedule_xlsx
        result = {
            "employees": [{"employee_id": "e1", "name": '=HYPERLINK("http://evil","click")',
                           "shifts": 1, "hours": 4, "max_hours": 40, "max_shifts": None,
                           "min_hours": None, "pay": 68.0, "roles_worked": ["grill"],
                           "available_but_unassigned": []}],
            "assignments": [{"employee_id": "e1", "employee_name": '=HYPERLINK("http://evil","click")',
                             "shift": "Mon_dinner", "day": "Mon", "window": "dinner",
                             "start": "17:00", "end": "21:00", "role": "grill",
                             "hours": 4, "wage": 17, "cost": 68.0}],
            "gaps": [], "labor_cost": 68.0,
        }
        p = os.path.join(tempfile.mkdtemp(), "schedule.xlsx")
        export_schedule_xlsx(result, p)
        wb = openpyxl.load_workbook(p)
        # invariant: no string cell anywhere begins with a formula-trigger character
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                for v in row:
                    if isinstance(v, str) and v:
                        self.assertNotIn(v[0], ("=", "+", "-", "@", "\t", "\r"),
                                         f"unsanitized cell in {ws.title}: {v!r}")

    def test_export_file_is_owner_only(self):
        from scheduler import export_schedule_xlsx
        result = {
            "employees": [{"employee_id": "e1", "name": "Alex", "shifts": 1, "hours": 4,
                           "max_hours": 40, "max_shifts": None, "min_hours": None, "pay": 68.0,
                           "roles_worked": ["grill"], "available_but_unassigned": []}],
            "assignments": [], "gaps": [], "labor_cost": 0.0,
        }
        p = os.path.join(tempfile.mkdtemp(), "schedule.xlsx")
        export_schedule_xlsx(result, p)
        mode = stat.S_IMODE(os.stat(p).st_mode)
        self.assertEqual(mode, 0o600, oct(mode))


class TestPIIFilePermissions(unittest.TestCase):
    def test_extracted_input_is_owner_only(self):
        import pipeline
        p = os.path.join(tempfile.mkdtemp(), "extracted_input.json")
        pipeline.save_extraction({"default_role_rates": {}}, [{"id": "e1", "name": "Alex"}], [], p)
        mode = stat.S_IMODE(os.stat(p).st_mode)
        self.assertEqual(mode, 0o600, oct(mode))


if __name__ == "__main__":
    unittest.main(verbosity=2)
