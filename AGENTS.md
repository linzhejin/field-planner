# Repository Guidelines

## Project Scope & Structure

This repository develops **农时窗口**, a rapeseed seedling-stage farm-task planner. The implementation provides configuration loading and environment diagnostics; weather integration, scheduling, model calls, and the product UI remain pending.

Project materials live in `AIC claude code/`:

- `农时窗口_项目说明_20261016.docx`: scope, decisions, responsibilities, acceptance criteria, and submission requirements. Read this first.
- `农时窗口_任务清单_优化版_20261016.docx`: current task checklist.
- `附件/`: official competition PDFs; preserve originals.
- `模板/`: official `.doc` original and editable `.docx` report template.

Keep Python code in `src/nongshi/` and tests in `tests/`. Root `README.md` documents setup; `pyproject.toml` and `uv.lock` define dependencies. Do not recreate the deleted historical proposals or duplicate the task checklist.

## Build, Test & Development Commands

Use Python 3.12 and uv from the repository root:

```powershell
uv sync --locked
uv run python -m nongshi.check_env
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

These install locked dependencies, check imports and temporary SQLite persistence, run tests, and check style. `uv run python -m streamlit hello` starts the framework demo, not the farm-planning application. Use `.venv`; avoid installing project packages into system or GIS environments.

## Style & Naming Conventions

Use UTF-8 Markdown with descriptive headings. Preserve Chinese document names and the existing `主题_用途_YYYYMMDD.docx` naming pattern. Use the editable report template for submission drafts; retain the official `.doc` unchanged.

Use four-space indentation, type hints, `snake_case` functions/modules, and `PascalCase` classes. Ruff uses a 100-character line limit; format with `uv run ruff format src tests`.

## Testing & Validation

Use pytest with `test_*.py` files and `test_*` functions. No coverage percentage is mandated. Tests use `.pytest_cache/tmp` to avoid shared temporary-directory permissions. For document changes, verify sources, preserve required fields, and inspect rendered pages.

Future business validation follows the project brief: missing-input clarification, scheduling constraints, citations, weather failures, and preservation of completed tasks. Configuration tests do not establish agronomic correctness.

## Commit & Pull Request Guidelines

There is no commit convention. Use Conventional Commits with task identifiers, such as `feat: T1.3 add timestamped weather data`. Commit or publish only when authorized.

PRs should describe the change, affected documents/modules, validation evidence, and limitations; link relevant tasks and include screenshots for interface or layout changes.

## Evidence & Configuration

Keep credentials in ignored `.env` files; distribute only `.env.example`. Preserve source dates and applicability conditions. Never invent weather, agronomic rules, surveys, or evaluation results. Remove school, college, and adviser identifiers from submissions.
