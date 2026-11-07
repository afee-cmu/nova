# Nova Manager features (additive local prototype)

Built against `afee-cmu/nova` `main` commit `18bc4bea3bcf1bd5d2bd589839ba9a2dcf4923ed`.
The original repository did not have a UI/API. This patch **does not** replace any existing code; copy the new files into the repo root and keep `.streamlit/config.toml` in its folder.

## Install and run

From the Nova repository directory, install `requirements-manager.txt` and run:

```bash
python -m pip install -r requirements-manager.txt
streamlit run nova_manager_app.py --server.address 127.0.0.1
```

Open the local address printed by Streamlit. **Do not deploy to a public host:** this local prototype has no authentication, authorization, customer separation, TLS termination, audit log, retention management, or rate limiting. A production launch requires those controls and a dedicated backend.

## Data flow

- **Schedule**: original `schedule_input_template.xlsx` with its existing worksheets; upload only here.
- **Menu & Live Tickets**: separate workbook with `Menu` sheet (`item`, `station`, `prep_minutes`) and optional `Tickets` sheet (`ticket_id`, `item`, `quantity`, `status`). Download a sample from the UI. Ticket status can be `queued`, `preparing`, `completed`, or `cancelled`. The UI also accepts manual ticket lines. Estimates update after ticket changes and count only active work.
- **Employee Profiles**: choose an employee from the schedule workbook, enter job-related observations, optionally send notes and *task names only* to OpenRouter for provisional ratings, review/edit ratings in the 1–10 range, then click **Approve**. The scheduling optimizer uses approved ratings for matching tasks. Unknown ratings continue to use existing workbook proficiency values (1–5) until changed.

Set `api_key` in the process environment (not in the UI or repository) if using AI rating suggestions. Ratings can also be entered manually without any API call. AI inference is fallible and managers must validate it. Employee notes and ratings are stored only in Streamlit session state and disappear on app restart/session expiry; there is no durable employee-profile database or POS integration yet. Ticket arrivals are manual or uploaded rather than live POS-fed. Wait time estimates are simplified, not promised customer wait times. Worker counts are input manually and are **not yet synchronized to on-duty assignments**.

## Security notes

Inputs are validated, have file and row count limits, and are preflight checked against zip bombs before parsing. Uploaded Excel files are deleted after parsing. No employee name/id is included in the AI rating prompt, but notes could still contain identifying information; never enter personal/sensitive details. External model processing requires appropriate privacy notice and vendor terms. All names/roles/notes remain data, not executable code. These features do not change or bypass existing hard scheduling constraints, FOH/BOH isolation, overtime flags, or capacity rules. Only the manager's approved task ratings alter the objective.

## Current-main compatibility caveat (checked Nov 7, 2026)

The upstream `main` branch advanced to `00f112f80662377bb27a38c382c707c669ebf2e8` and now includes `fire_times.py` and `expo.py`. They implement a more detailed kitchen planning engine with station **equipment capacity** and firing times. This prototype's Menu & Live Tickets tab instead uses a simplified per-worker backlog estimator. The two approaches are NOT connected, and the dashboard does NOT currently use the `fire_times.py` engine or automatically update the existing scheduler's busy-time weights. Both features can coexist as separate modules, but they should be reconciled before treating them as one production workflow.

The feature-unit tests passed in an isolated environment using a test-only implementation of the existing `schedule_input.preflight_upload` interface. Full upstream test-suite and live dashboard integration were **not** run in that environment because the full repo checkout and `ortools`/`streamlit` dependencies were unavailable. Run after copying these files into a current clone:

```bash
python -m pip install -r requirements-manager.txt
python -m unittest discover -s tests -p 'test_manager_features.py' -v
python -m unittest discover -s . -p 'test_*.py' -v
python -m compileall -q .
```

Do not merge or deploy if tests fail. Confirm the current `main` head and review any changes to the two kitchen-planning interfaces.
