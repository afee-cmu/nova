# BonApp

BonApp is a small-restaurant scheduling UI. Managers can upload a canonical Excel workbook or use the demo, review coverage and code-generated insights, update shifts, and approve proposed callout re-plans.

## Run

From the repository root, install dependencies:

```bash
python -m pip install fastapi uvicorn requests openpyxl ortools python-multipart
```

AI parsing is optional. In the same terminal, set `api_key` before starting Uvicorn to enable it; without the key, the keyword fallback is used:

```bash
export api_key="your-openrouter-api-key"
uvicorn ui.server:app --reload
```

For offline mode, omit the `export` line and run `uvicorn ui.server:app --reload`. Open <http://127.0.0.1:8000>. The demo workbook is [ui/demo_data/demo_input.xlsx](demo_data/demo_input.xlsx); regenerate it from the canonical template with `python ui/demo_data/make_demo_workbook.py`.

`solver_bridge.py` adapts workbook data with the teammates’ `load_workbook_input()` and `to_scheduler_inputs()`, calls `run_pipeline()` and normalizes its result for the UI. It retains those exact scheduler employees and config for callout re-planning through `reoptimize()`; `adapters.py` provides the keyword parsing fallback.

## Verify

- Use **Use demo data** or upload the demo workbook from Data In.
- Check `/api/health` and `/api/schedule` for service mode and schedule data.
- Send “Maria Alvarez called out Friday Dinner Rush”; approve or reject the re-plan, then try **Reset demo**.
