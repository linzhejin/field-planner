# Repository Guidelines

## Project Scope & Structure

This repository develops **农时窗口**, a rapeseed seedling-stage farm-task planner. The primary Streamlit workbench supports blank-first field setup, a versioned SQLite draft and event history, condition-aware rule evidence, Open-Meteo weather snapshots, seven-day minute scheduling, a DeepSeek tool-calling assistant, and preview-and-adopt plan revisions. Model and weather integrations must degrade cleanly to structured inputs when unavailable.

Project materials and source data live in:

- `AIC claude code/`: project brief, task checklist, and submission requirements.
- `附件/`: official competition PDFs; preserve originals.
- `模板/`: official `.doc` original and editable `.docx` report template.
- `rules/`: C rule cards, source index, legacy R mappings, D engineering conventions, and reference cases. Do not merge their identifiers or change source files casually.

Keep Python code in `src/nongshi/` and tests in `tests/`. Root `README.md` documents setup; `pyproject.toml` and `uv.lock` define dependencies. The main app is `src/nongshi/app.py`; `app_v2.py` remains available for offline regression checks. Do not recreate deleted historical proposals or duplicate the team task checklist.

## Build, Test & Development Commands

Use Python 3.12 and uv from the repository root:

```powershell
uv sync --locked
uv run python -m nongshi.check_env
uv run streamlit run src/nongshi/app.py
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

These install locked dependencies, check imports and temporary SQLite persistence, run the offline app, run tests, and check style. Use `.venv`; avoid installing project packages into system or GIS environments.

## Style & Naming Conventions

Use UTF-8 Markdown with descriptive headings. Preserve Chinese document names and the existing `主题_用途_YYYYMMDD.docx` naming pattern. Use the editable report template for submission drafts; retain the official `.doc` unchanged.

Use four-space indentation, type hints, `snake_case` functions/modules, and `PascalCase` classes. Ruff uses a 100-character line limit; format with `uv run ruff format src tests`.

## Testing & Validation

Use pytest with `test_*.py` files and `test_*` functions. No coverage percentage is mandated. Tests use `.pytest_tmp/` so fresh checkouts can run them. For document changes, verify sources, preserve required fields, and inspect rendered pages.

Business validation follows the project brief: missing-input clarification, scheduling constraints, source citations, weather failures, persistence conflicts, and preservation of completed tasks. Keep C11, C18, C19, C20, C23, C24, and C31 marked as development candidates pending review. Passing simulated tests does not establish agronomic correctness or live-service availability.

## Commit & Pull Request Guidelines

There is no commit convention. Use Conventional Commits with task identifiers, such as `feat: T1.3 add timestamped weather data`. Commit or publish only when authorized.

PRs should describe the change, affected documents/modules, validation evidence, and limitations; link relevant tasks and include screenshots for interface or layout changes.

## Evidence & Configuration

Keep credentials in ignored `.env` files; distribute only `.env.example`. The DeepSeek default is configurable and an empty key must leave structured workflows usable. Preserve source dates and applicability conditions. Never invent weather, agronomic rules, surveys, or evaluation results. Remove school, college, and adviser identifiers from submissions.
