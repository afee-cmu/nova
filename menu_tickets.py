"""Validated menu/ticket parsing and deterministic station backlog estimation.

No network calls, arbitrary code execution, or persistence. Estimates are workload
proxies, not guaranteed customer-ready times (parallel cooking/batching is simplified).
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime
from math import ceil, isfinite
from pathlib import Path
from tempfile import NamedTemporaryFile
import os

from openpyxl import load_workbook
from schedule_input import preflight_upload

MAX_ROWS = 5000
MAX_UPLOAD = 15 * 1024 * 1024
VALID_STATUSES = {"queued", "preparing", "completed", "cancelled"}


def _name(value, label, max_length=100):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > max_length:
        raise ValueError(f"{label} must be nonempty text of at most {max_length} characters")
    return value.strip()


def _positive_number(value, label, maximum):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{label} must be numeric") from e
    if not isfinite(number) or not 0 < number <= maximum:
        raise ValueError(f"{label} must be between 0 and {maximum}")
    return number


def parse_menu_rows(rows):
    """item, station, prep_minutes; duplicates and invalid rows fail closed."""
    result = {}
    for n, row in enumerate(rows, 2):
        if not any(v is not None and str(v).strip() for v in row[:3]):
            continue
        item = _name(row[0], f"Menu row {n}: item")
        station = _name(row[1], f"Menu row {n}: station")
        minutes = _positive_number(row[2], f"Menu row {n}: prep_minutes", 600)
        if item.casefold() in (k.casefold() for k in result):
            raise ValueError(f"Duplicate item: {item}")
        result[item] = {"station": station, "prep_minutes": minutes}
    if not result:
        raise ValueError("Menu sheet contains no items")
    return result


def parse_ticket_rows(rows, menu):
    """ticket_id, item, quantity, status. Multiple rows per ticket allowed."""
    result = []
    for n, row in enumerate(rows, 2):
        if not any(v is not None and str(v).strip() for v in row[:4]):
            continue
        ticket = _name(str(row[0]) if row[0] is not None else None, f"Tickets row {n}: ticket_id", 64)
        item = _name(row[1], f"Tickets row {n}: item")
        if item not in menu:
            raise ValueError(f"Tickets row {n}: unknown menu item '{item}'")
        q = _positive_number(row[2], f"Tickets row {n}: quantity", 1000)
        if not q.is_integer():
            raise ValueError(f"Tickets row {n}: quantity must be a whole number")
        status = str(row[3] or "queued").strip().lower()
        if status not in VALID_STATUSES:
            raise ValueError(f"Tickets row {n}: invalid status '{status}'")
        result.append({"ticket_id": ticket, "item": item, "quantity": int(q), "status": status})
    return result


def read_menu_workbook(blob):
    """Read isolated second .xlsx input with existing repo's zip preflight.

    Workbook must have Menu columns (item,station,prep_minutes), optionally Tickets
    (ticket_id,item,quantity,status). File is not retained after parsing.
    """
    if not isinstance(blob, bytes) or len(blob) > MAX_UPLOAD or not blob:
        raise ValueError("Upload must be a nonempty .xlsx file under 15 MB")
    path = None
    try:
        with NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            path = f.name
            os.chmod(path, 0o600)
            f.write(blob)
        error = preflight_upload(path)
        if error:
            raise ValueError(str(error))
        wb = load_workbook(path, read_only=True, data_only=True, keep_links=False)
        try:
            if "Menu" not in wb.sheetnames:
                raise ValueError("Missing Menu sheet")
            def sheet_rows(name, expected):
                ws = wb[name]
                if ws.max_row > MAX_ROWS + 1:
                    raise ValueError(f"{name} sheet has too many rows")
                lines = ws.iter_rows(values_only=True)
                header = tuple(str(v or "").strip().lower() for v in next(lines, ()))
                if header[:len(expected)] != tuple(expected):
                    raise ValueError(f"{name} must start with columns: {', '.join(expected)}")
                return list(lines)
            menu = parse_menu_rows(sheet_rows("Menu", ("item", "station", "prep_minutes")))
            tickets = parse_ticket_rows(sheet_rows("Tickets", ("ticket_id", "item", "quantity", "status")), menu) if "Tickets" in wb.sheetnames else []
            return menu, tickets
        finally:
            wb.close()
    finally:
        if path:
            Path(path).unlink(missing_ok=True)


def estimate_backlog(menu, tickets, workers_by_station=None):
    """Estimate queued workload by station and per-ticket completion time.

    Assumes each station has identical parallel workers and tasks are sequential
    within each station. The bottleneck station controls estimated ticket wait.
    """
    workers_by_station = workers_by_station or {}
    remaining = defaultdict(float)
    completion = {}
    for t in tickets:
        if t["status"] in ("completed", "cancelled"):
            continue
        spec = menu[t["item"]]
        station = spec["station"]
        workers = workers_by_station.get(station, 1)
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1 or workers > 500:
            raise ValueError(f"Worker count for {station} must be between 1 and 500")
        remaining[station] += spec["prep_minutes"] * t["quantity"]
        minutes = ceil(remaining[station] / workers)
        completion[t["ticket_id"]] = max(completion.get(t["ticket_id"], 0), minutes)
    stations = {station: {"work_minutes": round(work, 2),
                          "workers": workers_by_station.get(station, 1),
                          "estimated_clear_minutes": ceil(work / workers_by_station.get(station, 1))}
                for station, work in remaining.items()}
    return {"active_tickets": len({t["ticket_id"] for t in tickets if t["status"] not in ("completed", "cancelled")}),
            "stations": stations, "ticket_wait_minutes": completion,
            "estimated_clear_minutes": max((v["estimated_clear_minutes"] for v in stations.values()), default=0)}
