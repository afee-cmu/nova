# BonApp UI backend

## Run locally

From the repository root, install the backend dependencies:

```bash
python -m pip install fastapi uvicorn requests
```

For AI parsing, set the OpenRouter key in the same terminal before starting the server:

```bash
export api_key="your-openrouter-api-key"
uvicorn ui.server:app --reload
```

The key is read only by the backend and is never sent to the browser or written to a file. The repository `.gitignore` excludes `.env`; do not store the key in source code or another file. Without `api_key`, BonApp runs in offline mode and uses the keyword parser.

Open <http://127.0.0.1:8000/>.

## Verify

- Visit <http://127.0.0.1:8000/api/health> and confirm `ai_available` is `true` when `api_key` is set and `false` when it is absent.
- Visit <http://127.0.0.1:8000/api/schedule> and confirm it returns `schedule`, `explanation`, and `employees`.
- On the page, submit a free-form callout or cover message, and try “Who is working Friday dinner?” to check a schedule question.
- Check the reply's “Understood as” chip and source label to see the parsed action and whether AI or keyword parsing handled it.
