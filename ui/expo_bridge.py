"""Expo path: kitchen setup + tickets from camera photos or pasted text -> live fire queue.

Reuses expo.py (LLM prompts, validation, plain-English corrections) and fire_times.py (OR-Tools
fire-time planner). Kitchen setup is saved to kitchen.json so it's a one-time step; orders live in
memory for the current service.
"""

import base64
import binascii
import json
import os
import re
import threading
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KITCHEN_PATH = ROOT / "kitchen.json"
SAMPLE_KITCHEN = ROOT / "sample_kitchen_setup.txt"
SAMPLE_TICKETS = ROOT / "sample_tickets.txt"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 20_000
IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}


class ExpoError(ValueError):
    """A problem the expo/manager can fix."""


def _require_key():
    if not os.getenv("api_key"):
        raise ExpoError("Reading menus and tickets uses AI. Set the api_key environment variable and restart the server.")


# ---------------------------------------------------------------------------
# State (single process, in memory; kitchen persisted to kitchen.json)
# ---------------------------------------------------------------------------
_lock = threading.Lock()   # several tickets can be snapped back to back
state = {"kitchen": None, "kitchen_draft": None, "kitchen_history": [], "orders": [], "order_history": []}


def load_saved_kitchen():
    if KITCHEN_PATH.exists():
        try:
            data = json.loads(KITCHEN_PATH.read_text())
            from fire_times import validate_kitchen
            if not validate_kitchen(data.get("menu", {}), data.get("stations", {})):
                state["kitchen"] = data
        except (OSError, ValueError):
            state["kitchen"] = None


def samples():
    read = lambda p: p.read_text() if p.exists() else ""
    return {"kitchen_text": read(SAMPLE_KITCHEN), "tickets_text": read(SAMPLE_TICKETS)}


def kitchen_view(kitchen):
    if not kitchen:
        return None
    return {"stations": kitchen["stations"],
            "menu": [{"name": n, "station": m["station"], "cook_min": m["cook_min"], "space": m.get("space", 1)}
                     for n, m in kitchen["menu"].items()],
            "assumptions": kitchen.get("assumptions", [])}


def status():
    return {"kitchen": kitchen_view(state["kitchen"]),
            "draft": kitchen_view(state["kitchen_draft"]),
            "can_undo_kitchen": bool(state["kitchen_history"]),
            "orders": deepcopy(state["orders"]),
            "can_undo_orders": bool(state["order_history"])}


# ---------------------------------------------------------------------------
# Kitchen setup (one time)
# ---------------------------------------------------------------------------
def draft_kitchen(text):
    _require_key()
    text = (text or "").strip()
    if not text:
        raise ExpoError("Paste your menu (and a line about your equipment, if you like).")
    if len(text) > MAX_TEXT_CHARS:
        raise ExpoError(f"Keep the menu under {MAX_TEXT_CHARS:,} characters.")
    import expo
    try:
        kitchen = expo.ask_for_json(expo.KITCHEN_PROMPT, text, expo.check_kitchen)
    except RuntimeError as error:
        raise ExpoError(f"The menu couldn't be read reliably: {error}") from error
    kitchen["menu_text"] = text          # used later to split combos on tickets
    state["kitchen_draft"], state["kitchen_history"] = kitchen, []


def correct_kitchen(message):
    _require_key()
    draft = state["kitchen_draft"]
    if draft is None:
        raise ExpoError("Draft the kitchen setup first.")
    import expo
    current = {k: draft[k] for k in ("stations", "menu", "assumptions") if k in draft}
    try:
        updated = expo.ask_for_json(expo.CORRECTION_PROMPT,
                                    f"Current JSON:\n{json.dumps(current)}\n\nCorrection: {message}", expo.check_kitchen)
    except RuntimeError as error:
        raise ExpoError(f"Couldn't apply that: {error}") from error
    changes = expo.describe_kitchen_changes(current, updated)
    if changes:
        state["kitchen_history"].append(deepcopy(draft))
        updated["menu_text"] = draft.get("menu_text", "")
        state["kitchen_draft"] = updated
    return changes


def undo_kitchen():
    if not state["kitchen_history"]:
        return False
    state["kitchen_draft"] = state["kitchen_history"].pop()
    return True


def save_kitchen():
    if state["kitchen_draft"] is None:
        raise ExpoError("There's no kitchen draft to save.")
    state["kitchen"] = state["kitchen_draft"]
    state["kitchen_draft"], state["kitchen_history"] = None, []
    KITCHEN_PATH.write_text(json.dumps(state["kitchen"], indent=2))


def edit_kitchen():
    if state["kitchen"] is None:
        raise ExpoError("There's no saved kitchen to edit.")
    state["kitchen_draft"], state["kitchen_history"] = deepcopy(state["kitchen"]), []


# ---------------------------------------------------------------------------
# Tickets -> orders
# ---------------------------------------------------------------------------
def _decode_image(data_url):
    match = re.fullmatch(r"data:(image/[a-z]+);base64,([A-Za-z0-9+/=\s]+)", data_url or "")
    if not match or match.group(1) not in IMAGE_TYPES:
        raise ExpoError("The photo must be a JPEG, PNG or WebP image.")
    try:
        raw = base64.b64decode(match.group(2), validate=False)
    except (binascii.Error, ValueError) as error:
        raise ExpoError("The photo couldn't be decoded.") from error
    if len(raw) > MAX_IMAGE_BYTES:
        raise ExpoError("The photo is too large (8 MB max).")
    return match.group(1), base64.b64encode(raw).decode()


def _read(content_parts, now):
    import expo
    from fire_times import validate_kitchen

    kitchen = state["kitchen"]
    prompt = (expo.TICKET_PROMPT.replace("{NOW}", now)
              .replace("{ITEMS}", ", ".join(kitchen["menu"]))
              .replace("{MENU_TEXT}", kitchen.get("menu_text", "")))

    def check(data):
        if not isinstance(data.get("orders"), list) or not data["orders"]:
            return ["'orders' must be a non-empty list; return your best reading of every ticket shown"]
        return validate_kitchen(kitchen["menu"], kitchen["stations"], data["orders"])

    try:
        return expo.ask_for_json(prompt, content_parts, check)
    except RuntimeError as error:
        raise ExpoError(f"The ticket couldn't be read reliably. Try a closer, steadier photo. ({error})") from error


def add_tickets(now, image=None, text=None):
    """Read one photo or a block of ticket text and add the orders. Duplicates (same id) are skipped."""
    _require_key()
    if state["kitchen"] is None:
        raise ExpoError("Set up your kitchen first (one time).")
    parts = [{"type": "text", "text": "Tickets:"}]
    if image:
        mime, b64 = _decode_image(image)
        parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
    elif text and text.strip():
        if len(text) > MAX_TEXT_CHARS:
            raise ExpoError(f"Keep pasted tickets under {MAX_TEXT_CHARS:,} characters.")
        parts.append({"type": "text", "text": text})
    else:
        raise ExpoError("Snap a photo or paste a ticket.")

    parsed = _read(parts, now)          # slow AI call happens outside the lock
    with _lock:
        existing = {o["id"] for o in state["orders"]}
        added, skipped = [], []
        for o in parsed["orders"]:
            (skipped.append(o["id"]) if o["id"] in existing else added.append(o))
            existing.add(o["id"])
        if added:
            state["order_history"].append(deepcopy(state["orders"]))
            state["orders"].extend(added)
    return {"added": added, "skipped": skipped, "assumptions": parsed.get("assumptions", [])}


def correct_orders(message):
    _require_key()
    if not state["orders"]:
        raise ExpoError("There are no orders to correct.")
    import expo
    prompt = expo.ORDER_CORRECTION_PROMPT.replace("{ITEMS}", ", ".join(state["kitchen"]["menu"]))
    current = {"orders": state["orders"], "assumptions": []}
    try:
        updated = expo.ask_for_json(prompt, f"Current JSON:\n{json.dumps(current)}\n\nCorrection: {message}",
                                    expo.order_check(state["kitchen"]))
    except RuntimeError as error:
        raise ExpoError(f"Couldn't apply that: {error}") from error
    changes = expo.describe_order_changes(current, updated)
    with _lock:
        if changes:
            state["order_history"].append(deepcopy(state["orders"]))
            state["orders"] = updated["orders"]
    return changes


def undo_orders():
    if not state["order_history"]:
        return False
    state["orders"] = state["order_history"].pop()
    return True


def complete_order(order_id):
    with _lock:
        if not any(o["id"] == order_id for o in state["orders"]):
            return False
        state["order_history"].append(deepcopy(state["orders"]))
        state["orders"] = [o for o in state["orders"] if o["id"] != order_id]
        return True


def clear_orders():
    if state["orders"]:
        state["order_history"].append(deepcopy(state["orders"]))
    state["orders"] = []


def plan(now):
    """Fire queue for the current orders at time `now` (HH:MM)."""
    if state["kitchen"] is None or not state["orders"]:
        return None
    from fire_times import plan_fires
    k = state["kitchen"]
    result = plan_fires(k["menu"], k["stations"], state["orders"], now)
    return result if result.get("status") == "ok" else None
