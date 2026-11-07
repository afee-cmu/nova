"""Local-first manager dashboard. Not safe to publish as an unauthenticated website.

Run: streamlit run nova_manager_app.py --server.address 127.0.0.1
"""
import streamlit as st
from io import BytesIO
from tempfile import NamedTemporaryFile
from pathlib import Path
import os

from openpyxl import Workbook
from schedule_input import load_workbook_input, efficiency_inputs, preflight_upload
from efficiency_scheduler import solve_efficient_schedule, format_report
from menu_tickets import read_menu_workbook, estimate_backlog
from employee_profiles import suggest_ratings, merge_approved_ratings

st.set_page_config(page_title="Nova Manager", layout="wide")
st.title("Nova — Restaurant Operations")
st.caption("Local manager-only prototype. Do not expose this app to the internet without authentication and tenant isolation.")


def state(key, default):
    if key not in st.session_state:
        st.session_state[key] = default
    return st.session_state[key]


def make_template():
    wb = Workbook()
    ws = wb.active
    ws.title = "Menu"
    ws.append(["item", "station", "prep_minutes"])
    ws.append(["Cheeseburger", "grill", 8])
    ws.append(["Fries", "fryer", 5])
    ws = wb.create_sheet("Tickets")
    ws.append(["ticket_id", "item", "quantity", "status"])
    ws.append(["1001", "Cheeseburger", 2, "queued"])
    output = BytesIO()
    wb.save(output)
    return output.getvalue()


def load_schedule_upload(blob):
    if len(blob) > 15 * 1024 * 1024:
        raise ValueError("Schedule file exceeds 15 MB")
    tmp = None
    try:
        with NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            tmp = f.name
            os.chmod(tmp, 0o600)
            f.write(blob)
        issue = preflight_upload(tmp)
        if issue:
            raise ValueError(str(issue))
        data, issues = load_workbook_input(tmp)
        if issues.errors:
            raise ValueError("; ".join(issues.errors[:8]))
        return data, issues
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)

schedule_tab, menu_tab, profiles_tab = st.tabs(["Schedule", "Menu & Live Tickets", "Employee Profiles"])
with schedule_tab:
    st.subheader("Scheduling spreadsheet")
    schedule_file = st.file_uploader("Upload existing Nova scheduling workbook", type=["xlsx"], key="schedule_upload")
    if schedule_file is not None and st.button("Validate scheduling workbook"):
        try:
            data, issues = load_schedule_upload(schedule_file.getvalue())
            st.session_state["schedule_data"] = data
            st.session_state["approved_profiles"] = {}  # reset on a new roster
            st.success(f"Loaded {len(data['employees'])} employees; {len(data['demand'])} demand rows")
            for warning in issues.warnings[:5]:
                st.warning(str(warning))
        except Exception as exc:
            st.error(f"Invalid scheduling workbook: {exc}")
    data = st.session_state.get("schedule_data")
    if data:
        st.write("Approved rating profiles:", len(state("approved_profiles", {})))
        if st.button("Optimize by approved task strengths"):
            try:
                employees, config, extras = efficiency_inputs(data)
                extras = merge_approved_ratings(extras, state("approved_profiles", {}), data)
                result = solve_efficient_schedule(employees, config, **extras)
                st.session_state["latest_result"] = result
                st.code(format_report(result), language="text")
            except Exception as exc:
                st.error(f"Optimization failed: {exc}")

with menu_tab:
    st.subheader("Separate menu & tickets upload")
    st.download_button("Download menu/ticket template", make_template(), file_name="nova_menu_tickets_template.xlsx")
    file = st.file_uploader("Upload menu & ticket workbook (not your scheduling workbook)", type=["xlsx"], key="menu_upload")
    if file is not None and st.button("Load menu and tickets"):
        try:
            menu, tickets = read_menu_workbook(file.getvalue())
            st.session_state["menu"] = menu
            st.session_state["tickets"] = tickets
            st.success(f"Loaded {len(menu)} menu items and {len(tickets)} ticket lines")
        except Exception as exc:
            st.error(f"Invalid menu/ticket workbook: {exc}")
    menu = st.session_state.get("menu")
    if menu:
        st.dataframe([{"item": k, **v} for k, v in menu.items()], hide_index=True)
        with st.form("new_ticket"):
            ticket_id = st.text_input("Ticket number", max_chars=64)
            item = st.selectbox("Menu item", list(menu))
            quantity = st.number_input("Quantity", 1, 1000, 1, step=1)
            submitted = st.form_submit_button("Add ticket line")
            if submitted:
                if not ticket_id.strip():
                    st.error("Ticket number is required")
                else:
                    state("tickets", []).append({"ticket_id": ticket_id.strip(), "item": item,
                                                  "quantity": int(quantity), "status": "queued"})
                    st.success("Ticket line added")
        tickets = state("tickets", [])
        if tickets:
            st.dataframe(tickets, hide_index=True)
            active_ids = sorted({t["ticket_id"] for t in tickets if t["status"] not in ("completed", "cancelled")})
            if active_ids:
                done = st.selectbox("Ticket to complete", active_ids)
                if st.button("Mark ticket completed"):
                    for t in tickets:
                        if t["ticket_id"] == done:
                            t["status"] = "completed"
                    st.rerun()
        st.subheader("Estimated workload")
        workers = {}
        for station in sorted({v["station"] for v in menu.values()}):
            workers[station] = int(st.number_input(f"Workers at {station}", 1, 100, 1, key="workers_"+station))
        estimate = estimate_backlog(menu, tickets, workers)
        st.metric("Active tickets", estimate["active_tickets"])
        st.metric("Estimated time to clear active backlog", f"{estimate['estimated_clear_minutes']} min")
        st.dataframe([{"station": station, **item} for station, item in estimate["stations"].items()], hide_index=True)
        st.caption("Simplified estimates: preparation times are added per station and divided by workers; batching, overlapping cooking, delays, and equipment capacity are not modeled.")

with profiles_tab:
    st.subheader("Employee profiles and task ratings")
    data = st.session_state.get("schedule_data")
    if not data:
        st.info("First upload the Nova scheduling workbook in the Schedule tab to load employee IDs and eligible tasks.")
    else:
        employees = {e["employee_id"]: e for e in data["employees"]}
        eid = st.selectbox("Employee", list(employees), format_func=lambda x: f"{employees[x]['name']} ({x})")
        person = employees[eid]
        tasks = list(person["task_skills"])
        if not tasks:
            st.warning("This employee has no eligible tasks in the scheduling workbook.")
        else:
            notes = st.text_area("Manager performance observations (job-relevant only)", key="notes_" + eid, max_chars=2000,
                                 placeholder="Example: Handles high-volume orders accurately; needs coaching on dishwashing speed.")
            st.caption("AI notes are sent to OpenRouter without the employee's name or ID. Avoid entering medical, personal, or protected information.")
            if st.button("Suggest ratings with AI"):
                try:
                    st.session_state["draft_" + eid] = suggest_ratings(notes, tasks)
                    st.info("AI suggestions are not scheduled until you approve them.")
                except Exception as exc:
                    st.error(f"Could not generate ratings: {exc}")
            initial = state("draft_" + eid, {task: 5 for task in tasks})
            with st.form("ratings_" + eid):
                edited = {task: st.slider(task, 1, 10, int(initial.get(task, 5))) for task in tasks}
                approved = st.form_submit_button("Approve these ratings for scheduling")
                if approved:
                    state("approved_profiles", {})[eid] = edited
                    st.success("Approved. The next optimization will use these ratings.")
            if eid in state("approved_profiles", {}):
                st.write("Approved ratings:", state("approved_profiles", {})[eid])
