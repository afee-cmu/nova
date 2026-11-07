"""
Nova expo assistant: the AI layer around fire_times.py, built so the manager barely sets anything up.

  One-time setup:  paste the menu you already have (+ one line about your equipment).
                   The AI drafts each item's station and cook time and each station's
                   capacity; you correct only what's wrong, in plain English.
                   Saved to kitchen.json, so later runs skip setup entirely.
  During service:  paste ticket text or pass a photo of a tablet/printed ticket.
                   The AI turns it into orders (combos split, drinks dropped).
  Then code plans: fire queue, on-time/late status per order, station load.

Usage:
  uv run --with requests --with ortools --env-file .env \
      python expo.py sample_kitchen_setup.txt sample_tickets.txt --now 18:05
  Tickets can be several files, including photos: ... ticket1.jpg ticket2.png
  Flags: --now HH:MM (default: current time)   --reset (redo kitchen setup)   --yes (skip review)
"""

import base64
import json
import os
import re
import sys
from datetime import datetime

import requests

from fire_times import plan_fires, validate_kitchen

MODEL = "openai/gpt-4o"   # also reads photos of tickets
API_KEY = os.getenv("api_key")
URL = "https://openrouter.ai/api/v1/chat/completions"
KITCHEN_FILE = "kitchen.json"
MAX_FIX_ATTEMPTS = 2

KITCHEN_PROMPT = """You set up a kitchen-timing system from a restaurant's menu and a note about its equipment.
Output ONLY a JSON object, no prose:
{
  "stations": {"<station>": <how many spots it has, whole number>},
  "menu": {"<item name exactly as on the menu>": {"station": "<station>", "cook_min": <minutes>, "space": <spots, default 1>}},
  "assumptions": ["<every estimate or guess you made>"]
}
Rules:
- Stations are lowercase single words: "grill" (flat top, griddle, broiler), "fryer", "cold" (salads, shakes,
  desserts, cold prep), "oven" (pizza, baked). Only include stations this menu needs.
- cook_min = how long ONE portion occupies its station once started, realistic for a busy casual restaurant
  (e.g. smash burger ~6, fries ~4, 10 wings ~12, salad ~3).
- Capacity is measured in spots. Use the equipment note: a fryer basket is about 1 spot; a flat top's spots are
  how many standard items fit. If something isn't stated, assume grill 6, fryer 4, cold 1 (per person),
  oven 4, and say so.
- "space" = how many spots ONE portion takes. Standard items take 1. Bigger items take more: if the grill fits
  6 single burgers but only 3 doubles, a double takes 2. Use 1 unless the menu or note makes a bigger item clear.
- Leave out items with no kitchen prep (fountain drinks, bottled drinks) and combos/meals (orders are split
  into their parts later). List what you left out in "assumptions".
"""

CORRECTION_PROMPT = """You update a kitchen-timing setup JSON based on a manager's correction.
Apply ONLY what the correction asks; keep everything else exactly the same. Output ONLY the full updated JSON
object in the same format: {"stations": {...}, "menu": {...}, "assumptions": [...]}.
- Station capacity is in spots; each menu item's "space" is how many spots one portion takes (default 1).
  Capacity statements about different items mean different space values. Example: "the grill fits 6 smash
  burgers or 3 double smashes, any mix" -> grill = 6, Smash Burger space = 1, Double Smash space = 2.
- Station names are lowercase single words. Every menu item's station must exist in "stations".
- Rewrite any assumption the correction makes wrong, and add one noting the manager's correction."""

ORDER_CORRECTION_PROMPT = """You update a list of restaurant orders (JSON) based on the expo's correction.
Apply ONLY what the correction asks; keep everything else exactly the same. Output ONLY the full updated JSON
object in the same format: {"orders": [...], "assumptions": [...]}.
- Valid menu item names (EXACT strings): {ITEMS}
- Times are 24-hour HH:MM (dinner-time "6:30" = 18:30); pickup can be "ASAP".
- Rewrite any assumption the correction makes wrong, and add one noting the correction."""

TICKET_PROMPT = """You turn restaurant order tickets (pasted text and/or photos of tablet screens or printed tickets)
into JSON for a kitchen-timing system. The current time is {NOW}.
Output ONLY a JSON object, no prose:
{
  "orders": [{"id": "<order number from the ticket, e.g. DD-1042; invent a short one if none>",
              "channel": "<DoorDash | Uber Eats | Grubhub | Phone | In-store | Website | Other>",
              "placed": "HH:MM",
              "pickup": "HH:MM or ASAP",
              "items": [{"name": "<EXACT menu item name>", "qty": <whole number>}]}],
  "assumptions": ["<every interpretation you made>"]
}
Rules:
- Valid menu item names (use these EXACT strings): {ITEMS}
- Split combos/meals into their kitchen items using this menu text:
{MENU_TEXT}
- Combo parts ADD to items listed separately on the same ticket: a "#2 Combo" (Chicken Sandwich + Fries) plus a
  separate "Fries" line = 2x Fries. Count every portion; never merge them away.
- Drop items not in the valid list (drinks, sauces, utensils) and note them in "assumptions".
- Ignore modifiers ("no pickles", "spicy", "BBQ") for timing.
- Times are 24-hour HH:MM; dinner-time tickets like "6:20" mean 18:20. "Dasher arriving", "courier ETA",
  "pickup by" give the pickup time. "In about 25 min" means placed time + 25. Dine-in or walk-in with no time = ASAP.
- If no placed time is shown, use {NOW}.
"""


# ---------------------------------------------------------------------------
# LLM helpers (accepts text and images)
# ---------------------------------------------------------------------------
def call_llm(system, content, history=None):
    messages = [{"role": "system", "content": system}] + (history or []) + [{"role": "user", "content": content}]
    r = requests.post(URL, headers={"Authorization": f"Bearer {API_KEY}"},
                      json={"model": MODEL, "messages": messages, "temperature": 0}, timeout=120)
    data = r.json()
    if "choices" not in data:
        raise RuntimeError(f"OpenRouter error: {data}")
    return data["choices"][0]["message"]["content"]


def parse_json(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in the reply")
    return json.loads(match.group(0))


def ask_for_json(system, content, check):
    """Ask for JSON; if it doesn't parse or `check` finds problems, send them back and retry."""
    history, prompt, errors = [], content, []
    for attempt in range(1, MAX_FIX_ATTEMPTS + 2):
        reply = call_llm(system, prompt, history)
        try:
            data = parse_json(reply)
            errors = check(data)
        except (ValueError, json.JSONDecodeError) as e:
            errors = [f"invalid JSON: {e}"]
        if not errors:
            return data
        print(f"    attempt {attempt}: {len(errors)} problem(s), asking the model to fix them")
        history += [{"role": "user", "content": prompt}, {"role": "assistant", "content": reply}]
        prompt = "Your JSON had these problems:\n- " + "\n- ".join(errors) + "\nReturn the full corrected JSON object only."
    raise RuntimeError("The model could not produce valid JSON:\n- " + "\n- ".join(errors))


def ticket_content(paths, intro):
    """Build a message from text files and images (photos of tablets or printed tickets)."""
    parts = [{"type": "text", "text": intro}]
    for p in paths:
        ext = os.path.splitext(p)[1].lower()
        if ext in (".jpg", ".jpeg", ".png", ".webp", ".heic"):
            fmt = {"jpg": "jpeg", "heic": "heic"}.get(ext[1:], ext[1:])
            data = base64.b64encode(open(p, "rb").read()).decode()
            parts.append({"type": "image_url", "image_url": {"url": f"data:image/{fmt};base64,{data}"}})
        else:
            parts.append({"type": "text", "text": open(p).read()})
    return parts


# ---------------------------------------------------------------------------
# Kitchen setup (one time)
# ---------------------------------------------------------------------------
def check_kitchen(k):
    if not isinstance(k.get("stations"), dict) or not isinstance(k.get("menu"), dict) or not k["menu"]:
        return ["need 'stations' and a non-empty 'menu' object"]
    return validate_kitchen(k["menu"], k["stations"])


def print_kitchen(k):
    print("    Stations: " + ", ".join(f"{s} ({n} spot{'s' if n != 1 else ''})" for s, n in k["stations"].items()))
    for name, mi in k["menu"].items():
        space = mi.get("space", 1)
        extra = f"  takes {space} spots" if space > 1 else ""
        print(f"      {name:<20} {mi['station']:<6} {mi['cook_min']:g} min{extra}")


def describe_kitchen_changes(old, new):
    lines = []
    for s in sorted(set(old["stations"]) | set(new["stations"])):
        a, b = old["stations"].get(s), new["stations"].get(s)
        if a != b:
            lines.append(f"Station {s}: " + ("added" if a is None else "removed" if b is None else f"{a} -> {b} spots"))
    for item in sorted(set(old["menu"]) | set(new["menu"])):
        a, b = old["menu"].get(item), new["menu"].get(item)
        if a is None:
            lines.append(f"Added {item} ({b['station']}, {b['cook_min']:g} min)")
        elif b is None:
            lines.append(f"Removed {item}")
        elif a != b:
            changes = []
            if a["station"] != b["station"]:
                changes.append(f"station {a['station']} -> {b['station']}")
            if a["cook_min"] != b["cook_min"]:
                changes.append(f"{a['cook_min']:g} -> {b['cook_min']:g} min")
            if a.get("space", 1) != b.get("space", 1):
                changes.append(f"takes {a.get('space', 1)} -> {b.get('space', 1)} spots")
            lines.append(f"{item}: " + ", ".join(changes))
    return lines


def review(data, prompt, check, describe, show, hint, example):
    """Shared correction loop: plain-English correction -> AI update -> validated -> code diff. 'undo' reverts."""
    history = []
    while True:
        text = input(f'\n    Correct anything (e.g. {example}), "undo", or press Enter to accept:\n    > ').strip()
        if not text:
            return data
        if text.lower() == "undo":
            if history:
                data = history.pop()
                print("    Undid the last change.")
                show(data)
            else:
                print("    Nothing to undo.")
            continue
        try:
            updated = ask_for_json(prompt, f"Current JSON:\n{json.dumps(data)}\n\nCorrection: {text}", check)
        except RuntimeError as e:
            print(f"    Couldn't apply that: {e}")
            continue
        changes = describe(data, updated)
        if not changes:
            print(f"    Nothing changed. {hint}")
            continue
        history.append(data)
        data = updated
        print("    Updated:")
        for line in changes:
            print(f"      {line}")


def review_kitchen(kitchen):
    return review(kitchen, CORRECTION_PROMPT, check_kitchen, describe_kitchen_changes, print_kitchen,
                  'Try naming the item or station, e.g. "fries take 3 minutes".',
                  '"wings take 14 minutes", "the flat top fits 6 smash burgers or 3 doubles"')


def setup_kitchen(setup_path, confirm=True):
    print("Setting up your kitchen (one time)...")
    kitchen = ask_for_json(KITCHEN_PROMPT, open(setup_path).read(), check_kitchen)
    print("\n    Here's my draft:")
    print_kitchen(kitchen)
    for a in kitchen.get("assumptions", []):
        print(f"      * {a}")
    if confirm:
        kitchen = review_kitchen(kitchen)
    with open(KITCHEN_FILE, "w") as f:
        json.dump(kitchen, f, indent=2)
    print(f"    Saved to {KITCHEN_FILE}. Next time setup is skipped.\n")
    return kitchen


# ---------------------------------------------------------------------------
# Tickets -> orders
# ---------------------------------------------------------------------------
def read_tickets(paths, kitchen, menu_text, now):
    prompt = (TICKET_PROMPT.replace("{NOW}", now)
              .replace("{ITEMS}", ", ".join(kitchen["menu"]))
              .replace("{MENU_TEXT}", menu_text))

    def check(data):
        if not isinstance(data.get("orders"), list) or not data["orders"]:
            return ["'orders' must be a non-empty list"]
        return validate_kitchen(kitchen["menu"], kitchen["stations"], data["orders"])

    return ask_for_json(prompt, ticket_content(paths, "Tickets:"), check)


def order_check(kitchen):
    def check(data):
        if not isinstance(data.get("orders"), list) or not data["orders"]:
            return ["'orders' must be a non-empty list"]
        return validate_kitchen(kitchen["menu"], kitchen["stations"], data["orders"])
    return check


def print_orders(parsed):
    for o in parsed["orders"]:
        items = ", ".join(f"{it['qty']}x {it['name']}" for it in o["items"])
        print(f"    {o['id']:<9} {o['channel']:<9} placed {o['placed']}  pickup {o['pickup']:<5}  {items}")


def describe_order_changes(old, new):
    old_by = {o["id"]: o for o in old["orders"]}
    new_by = {o["id"]: o for o in new["orders"]}
    lines = []
    for oid, o in new_by.items():
        p = old_by.get(oid)
        if p is None:
            lines.append(f"Added order {oid}")
            continue
        changes = []
        for field in ("channel", "placed", "pickup"):
            if o.get(field) != p.get(field):
                changes.append(f"{field} {p.get(field)} -> {o.get(field)}")
        a = {it["name"]: it.get("qty", 1) for it in p["items"]}
        b = {it["name"]: it.get("qty", 1) for it in o["items"]}
        for name in sorted(set(a) | set(b)):
            if a.get(name, 0) != b.get(name, 0):
                changes.append(f"{name} {a.get(name, 0)} -> {b.get(name, 0)}")
        if changes:
            lines.append(f"{oid}: " + "; ".join(changes))
    lines += [f"Removed order {oid}" for oid in old_by if oid not in new_by]
    return lines


def review_orders(parsed, kitchen):
    prompt = ORDER_CORRECTION_PROMPT.replace("{ITEMS}", ", ".join(kitchen["menu"]))
    return review(parsed, prompt, order_check(kitchen), describe_order_changes, print_orders,
                  'Try naming the order and item, e.g. "Grubhub has 2 fries".',
                  '"grubhub has 2 fries", "the phone order is picking up at 6:40"')


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    flags = {a for a in args if a in ("--reset", "--yes")}
    now = datetime.now().strftime("%H:%M")
    if "--now" in args:
        i = args.index("--now")
        now = args[i + 1]
        args = args[:i] + args[i + 2:]
    files = [a for a in args if a not in flags]
    if len(files) < 2:
        sys.exit("Usage: python expo.py <kitchen_setup.txt> <tickets...> [--now HH:MM] [--reset] [--yes]")
    if not API_KEY:
        sys.exit("No api_key found. Run with --env-file .env")
    setup_path, ticket_paths = files[0], files[1:]

    if os.path.exists(KITCHEN_FILE) and "--reset" not in flags:
        kitchen = json.load(open(KITCHEN_FILE))
        print(f"Using saved kitchen setup ({KITCHEN_FILE}); add --reset to redo it.")
    else:
        kitchen = setup_kitchen(setup_path, confirm="--yes" not in flags)

    print(f"Reading {len(ticket_paths)} ticket file(s) at {now}...")
    parsed = read_tickets(ticket_paths, kitchen, open(setup_path).read(), now)
    print_orders(parsed)
    for a in parsed.get("assumptions", []):
        print(f"      * {a}")
    if "--yes" not in flags:
        parsed = review_orders(parsed, kitchen)
    orders = parsed["orders"]

    print("\nPlanning...")
    plan = plan_fires(kitchen["menu"], kitchen["stations"], orders, now)
    if plan["status"] != "ok":
        sys.exit("Could not build a plan.")

    print(f"\nFIRE QUEUE (now {now})")
    for f in plan["fire_queue"]:
        print(f"  {f['fire_at']}  FIRE {f['qty']}x {f['item']:<17} [{f['station']}]  "
              f"for {f['order']} ({f['channel']}), order ready {f['order_ready']}")
    print("\nORDERS")
    for o in plan["orders"]:
        print(f"  {o['id']:<9} {o['channel']:<9} pickup {o['pickup']:<5} ready {o['ready']}  {o['status']}")
    print("\nSTATIONS")
    for s, b in plan["stations"].items():
        print(f"  {s:<6} {b['message']}")


if __name__ == "__main__":
    main()
