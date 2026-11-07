# Security notes

This is an iterative, defensive review of everything on `main`. It covers the current
library/CLI code; when a UI/API is added, re-run this review against that layer (see the
checklist at the end). Findings were reached by adversarial code review ("simulated
attacks" / threat modeling), **not** by running live exploits or sending real data anywhere.

## What the system does with data (trust boundaries)

```
 manager's files ─┬─ structured .xlsx ──► schedule_input.py ─┐
                  │   (no LLM; stays local)                   ├─► scheduler.py ─► schedule.xlsx
                  └─ free-text notes ───► pipeline.py ──LLM──►┘        reoptimize.py
                                           │
                                           └──► OpenRouter (THIRD PARTY) over HTTPS
```

- **Secrets:** the OpenRouter API key is read only from the `api_key` environment variable
  (`os.getenv("api_key")`), never hard-coded, logged, or written to a file. `.env*` is
  git-ignored. Provide it at run time (e.g. `--env-file .env`); never commit it.
- **PII / sensitive data:** employee names, wages, hours and availability. These appear in
  `extracted_input.json` and `schedule.xlsx`, and — on the `pipeline.py` path only — are sent
  to OpenRouter for extraction.

## Attack surfaces reviewed and current posture

| # | Surface | Risk | Status |
|---|---------|------|--------|
| 1 | API key leakage | key in logs / errors / files / git | **Low.** env-only; errors truncated to the provider's message (`[:500]`); `.env*` ignored; regression test scans the tree for secret literals. |
| 2 | PII to third-party LLM | `pipeline.py` sends names/wages to OpenRouter | **Documented, by design.** The deterministic `.xlsx` path (`schedule_input.py`) does **not** use any LLM — prefer it when data must stay in-house. Disclose OpenRouter processing to customers; see checklist. |
| 3 | PII at rest | `extracted_input.json`, `schedule.xlsx` unencrypted | **Hardened.** Both written owner-only (`0600`) and git-ignored. Full-disk/volume encryption and a retention/cleanup policy are deployment concerns. |
| 4 | Prompt injection via uploaded notes | malicious text steers the LLM | **Bounded.** LLM output is **code-validated** (`validate_input`, `check_setup`, JSON schema) and never `eval`'d; the key/secrets are never in the LLM context; a human review step (`review_loop`) shows a code-generated diff before solving. Worst case is bad roster data, caught by validation/review — not code execution or secret disclosure. |
| 5 | Spreadsheet formula injection | a name like `=HYPERLINK("http://evil",…)` executing when the exported file is opened | **Fixed.** Every string cell on export is neutralized (leading `= + - @ \t \r` → prefixed with `'`). Covered by a round-trip test. |
| 6 | Malicious `.xlsx` upload | zip bomb / oversized sheet → memory exhaustion | **Fixed.** `preflight_upload()` runs before openpyxl: rejects non-zips, files > 15 MB, >200 MB decompressed, or compression ratio > 200×. |
| 7 | DoS / hangs | request with no timeout | **Fixed.** `chat.py` and `pipeline.py` both set HTTP timeouts; solver has a time limit. |
| 8 | Dangerous sinks | `eval`/`exec`/`pickle`/`subprocess`/`shell`/`yaml.load`/`verify=False` | **None present.** Enforced by a regression test. |
| 9 | Supply chain | `requests`, `ortools`, `pandas`, `openpyxl` | Keep current (openpyxl ships XXE / quadratic-blowup fixes). Pin versions in a lockfile at deploy time. |

Regression tests for the above live in `test_security.py` (local, no network).

## Not in scope yet (no UI/API exists)

Authentication, authorization, multi-tenant isolation, rate limiting, audit logging, CSRF,
session management, and transport/at-rest encryption for a server are **not** implemented
because there is no server yet. They are mandatory before third parties connect — see below.

## Checklist before a UI / API ships (re-run the review then)

- [ ] **AuthN/AuthZ** on every endpoint; never trust client-supplied tenant/employee ids.
- [ ] **Tenant isolation**: one customer can never read another's employees/schedules.
- [ ] **Secrets** from a managed secret store (not env files on disk); per-tenant keys if used.
- [ ] **Encryption** in transit (TLS) and at rest for all PII; encrypt or avoid persisting
      `extracted_input.json` / `schedule.xlsx`; set a retention + deletion policy.
- [ ] **PII minimization**: send the LLM the least data possible; offer a no-LLM (xlsx-only)
      mode for customers who won't share PII with a third party; sign a DPA with the LLM vendor
      and disable provider-side training/retention.
- [ ] **Upload limits** enforced server-side (size, type, rate) in addition to `preflight_upload`.
- [ ] **Output handling**: keep the formula-injection sanitizer; set download content-type and
      `Content-Disposition`; scan/limit file sizes.
- [ ] **Rate limiting** and quotas on LLM-backed endpoints (cost + abuse).
- [ ] **Audit logging** of who viewed/changed schedules (without logging secrets or raw PII).
- [ ] **Dependency pinning** + automated vulnerability scanning in CI.
