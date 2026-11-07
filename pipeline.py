"""
End-to-end pipeline: messy manager documents -> LLM -> validated JSON -> solver -> Excel + summary.

  1. LLM reads the restaurant setup note   -> config (role rates + rush windows)
  2. LLM reads the availability document   -> employee profiles (exact shift names)
     Both steps are validated in code; any errors go back to the LLM to fix.
  3. scheduler.run_pipeline                 -> optimal schedule
  4. LLM writes a plain-English summary     -> added to the Excel file

Usage:
  uv run --with requests --with ortools --with pandas --with openpyxl --env-file .env \
      python pipeline.py sample_restaurant_setup.txt sample_availability.txt
"""

import json
import os
import re
import sys
from collections import defaultdict

import requests

from scheduler import DAY_ORDER, build_shifts, export_schedule_xlsx, run_pipeline, validate_input

MODEL = "openai/gpt-4o-mini"   # swap for a stronger model if extraction gets things wrong
API_KEY = os.getenv("api_key")
URL = "https://openrouter.ai/api/v1/chat/completions"
MAX_FIX_ATTEMPTS = 2

SETUP_PROMPT = """You convert a restaurant manager's description of their week into JSON.
Output ONLY a JSON object, no prose, in exactly this shape:
{
  "default_role_rates": {"<role>": <hourly wage number>},
  "rush_profile": {
    "weekday": [{"name": "<window name>", "window": "HH:MM-HH:MM", "staff": {"<role>": <count>}}],
    "weekend": [ ...same shape... ],
    "<Mon|Tue|Wed|Thu|Fri|Sat|Sun>": [ ...only if that specific day differs... ]
  },
  "assumptions": ["<anything you had to interpret or guess>"]
}
Rules:
- Roles are lowercase single words: use "grill" for cooks/line, "cashier" for register/front counter, "expo" for expo/expediting, unless the manager clearly uses other roles.
- Window names are lowercase single words like "lunch", "dinner", "late". Names must be unique within a day.
- Times are 24-hour HH:MM ("5 to 9" at dinner means 17:00-21:00).
- "weekday" covers Mon-Fri and "weekend" covers Sat-Sun. Add a specific day key ONLY when the manager says that day differs. It fully replaces the group for that day, so include ALL of that day's windows.
- Every role used in "staff" must appear in "default_role_rates".
"""

AVAILABILITY_PROMPT = """You convert a manager's employee roster and availability notes into JSON for a scheduling system.
Output ONLY a JSON object, no prose, in exactly this shape:
{
  "employees": [
    {
      "id": "emp_001",
      "name": "<name>",
      "roles": {"<role>": <hourly wage number or null>},
      "max_hours": <number>,
      "max_shifts": <number or null>,
      "available": ["<shift name>"]
    }
  ],
  "assumptions": ["<anything you had to interpret or guess>"]
}
Rules:
- Valid roles: {ROLES}. Map synonyms ("cook"/"line cook" -> grill, "register"/"front of house" -> the matching front roles). If someone's job matches no valid role (e.g. dishwasher), leave them out and say so in "assumptions".
- If the notes list several roles for one person ("cashier or expo", "cook, also trained on expo", "front of house (cashier or expo)"), include EVERY one of them.
- A role's wage is that employee's own rate if the notes give one; otherwise null ("regular rate", "standard pay" and unstated all mean null).
- Valid shift names. Use these EXACT strings and nothing else:
{SHIFTS}
- Interpret availability against those shifts: "lunches"/"mornings"/"days" -> lunch shifts; "nights"/"evenings" -> dinner shifts; "all day" -> every shift that day; "weekends" -> Sat and Sun; "weekdays" -> Mon-Fri; "open"/"anytime" -> every shift. Apply exclusions ("except Sundays", "not Saturday lunch").
- If no hour cap is given, use 40. If no shift cap is given, use null.
- Ids are emp_001, emp_002, ... in roster order.
- Record every interpretation in "assumptions" (e.g. "Read 'nights' as dinner shifts for Alex").
"""

SUMMARY_PROMPT = """You explain a weekly staff schedule to a busy restaurant manager.
From the JSON you are given, write a short summary (under 200 words) in plain text: no markdown, no asterisks, no tables.
- State the total labor cost and who carries the most hours.
- For every coverage gap, give the suggestion from that gap's "candidates" list. Use ONLY the people and reasons listed there. Never suggest anyone who isn't listed for that gap. If a gap has no candidates, say nobody on staff has that role and suggest hiring, cross-training, or lowering that window's staffing.
Be specific and brief."""


# ---------------------------------------------------------------------------
# LLM helpers
# ---------------------------------------------------------------------------
def call_llm(system, user, history=None):
    messages = [{"role": "system", "content": system}] + (history or []) + [{"role": "user", "content": user}]
    r = requests.post(
        URL,
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={"model": MODEL, "messages": messages},
        timeout=120,
    )
    data = r.json()
    if "choices" not in data:
        raise RuntimeError(f"OpenRouter error: {data}")
    return data["choices"][0]["message"]["content"]


def parse_json(text):
    """Pull the JSON object out of a reply, even if the model wrapped it in ``` fences or prose."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError("no JSON object found in the reply")
    return json.loads(match.group(0))


def ask_for_json(system, user, check):
    """Ask for JSON; if it doesn't parse or `check` finds problems, send them back and retry."""
    history, prompt, errors = [], user, []
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
        prompt = ("Your JSON had these problems:\n- " + "\n- ".join(errors)
                  + "\nReturn the full corrected JSON object only.")
    raise RuntimeError("The model could not produce valid JSON:\n- " + "\n- ".join(errors))


# ---------------------------------------------------------------------------
# Checks run on the LLM's output (code, not the LLM, decides what's valid)
# ---------------------------------------------------------------------------
def check_setup(cfg):
    errors = [f"missing '{k}'" for k in ("default_role_rates", "rush_profile") if k not in cfg]
    if errors:
        return errors
    for role, rate in cfg["default_role_rates"].items():
        if not isinstance(rate, (int, float)) or rate <= 0:
            errors.append(f"default rate for '{role}' must be a positive number")
    for key, windows in cfg["rush_profile"].items():
        if key not in ("weekday", "weekend", *DAY_ORDER):
            errors.append(f"rush_profile key '{key}' must be weekday, weekend, or one of {DAY_ORDER}")
        names = [w.get("name") for w in windows]
        if len(names) != len(set(names)):
            errors.append(f"rush_profile '{key}' repeats a window name: {names}")
    try:
        shifts, demand = build_shifts(cfg["rush_profile"])
    except Exception as e:
        return errors + [f"rush_profile could not be read ({e}); windows must look like '17:00-21:00'"]
    if not shifts:
        errors.append("rush_profile produced no shifts")
    return errors + validate_input([], cfg, shifts, demand)


def make_employee_check(cfg, shifts, demand):
    def check(data):
        if not isinstance(data.get("employees"), list) or not data["employees"]:
            return ["'employees' must be a non-empty list"]
        return validate_input(data["employees"], cfg, shifts, demand)
    return check


# ---------------------------------------------------------------------------
# Gap suggestions, computed in code so the LLM can't invent them
# ---------------------------------------------------------------------------
def gap_candidates(result, employees, shifts, limit=3):
    """For each gap, list who could cover it and what it would take, best options first."""
    usage = {e["employee_id"]: e for e in result["employees"]}
    working = defaultdict(dict)  # employee_id -> {shift: role}
    for a in result["assignments"]:
        working[a["employee_id"]][a["shift"]] = a["role"]

    out = []
    for gap in result["gaps"]:
        shift, role = gap["shift"], gap["role"]
        shift_hours = shifts[shift]["minutes"] / 60
        options = []
        for e in employees:
            if role not in e["roles"]:
                continue
            used = usage[e["id"]]
            new_hours = used["hours"] + shift_hours
            if shift in working[e["id"]]:
                reason, rank = f"already working this shift as {working[e['id']][shift]}", 3
            elif new_hours > e["max_hours"]:
                reason = f"would go over their hour cap ({used['hours']:g} + {shift_hours:g} > {e['max_hours']:g})"
                rank = 2
            elif e.get("max_shifts") is not None and used["shifts"] + 1 > e["max_shifts"]:
                reason, rank = f"would go over their {e['max_shifts']}-shift cap", 2
            elif shift not in e["available"]:
                reason, rank = "has spare hours but would need to add this shift to their availability", 1
            else:
                reason, rank = "can take it", 0
            options.append((rank, used["hours"], {"name": e["name"], "reason": reason}))
        options.sort(key=lambda o: (o[0], o[1]))
        out.append({**gap, "candidates": [o[2] for o in options[:limit]]})
    return out


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(setup_path, availability_path, xlsx_path="schedule.xlsx"):
    if not API_KEY:
        sys.exit("No api_key found. Run with --env-file .env")

    setup_text = open(setup_path).read()
    availability_text = open(availability_path).read()

    print("1/4 Reading restaurant setup...")
    config = ask_for_json(SETUP_PROMPT, setup_text, check_setup)
    shifts, demand = build_shifts(config["rush_profile"])
    print(f"    {len(shifts)} shifts per week; roles: {', '.join(config['default_role_rates'])}")

    print("2/4 Reading employee availability...")
    prompt = (AVAILABILITY_PROMPT
              .replace("{ROLES}", ", ".join(config["default_role_rates"]))
              .replace("{SHIFTS}", ", ".join(shifts)))
    roster = ask_for_json(prompt, availability_text, make_employee_check(config, shifts, demand))
    employees = roster["employees"]
    print(f"    {len(employees)} employees: {', '.join(e['name'] for e in employees)}")

    assumptions = config.get("assumptions", []) + roster.get("assumptions", [])
    with open("extracted_input.json", "w") as f:
        json.dump({"config": config, "employees": employees, "assumptions": assumptions}, f, indent=2)

    print("3/4 Solving schedule...")
    result = run_pipeline(employees, config)
    if result["status"] not in ("optimal", "feasible"):
        sys.exit(f"Could not build a schedule: {result}")

    print("4/4 Writing summary...")
    summary_input = {
        "labor_cost": result["labor_cost"],
        "hours_by_employee": {e["name"]: e["hours"] for e in result["employees"]},
        "gaps": gap_candidates(result, employees, shifts),
    }
    summary = call_llm(SUMMARY_PROMPT, json.dumps(summary_input)).replace("**", "")
    if assumptions:
        summary += "\n\nHow your notes were interpreted:\n" + "\n".join(f"- {a}" for a in assumptions)
    export_schedule_xlsx(result, xlsx_path, summary=summary)

    print("\n" + summary)
    print(f"\nSaved {xlsx_path} (schedule) and extracted_input.json (what the LLM extracted)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python pipeline.py <restaurant_setup.txt> <availability.txt>")
    main(sys.argv[1], sys.argv[2])
