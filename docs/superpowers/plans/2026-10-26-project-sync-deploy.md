# Project Organization, Sync, and Deployment Plan

> **For agentic workers:** Execute this plan in the current `main` checkout. Preserve unrelated user work and do not force-push.

**Goal:** Reconcile the verified workbench and project evidence with the synchronized remotes, create a reviewable commit, and make the supported deployment available.

**Architecture:** Compare the current checkout and all uncommitted files against `origin/main` before integration. Keep the repository's existing Streamlit entrypoint and SQLite isolation; do not introduce a new hosting platform or change evaluation evidence.

**Tech Stack:** Python 3.12, uv, Streamlit, pytest, Ruff, Git, GitHub `origin/main`, Gitee `gitee/master`.

## Global Constraints

- Preserve `.env`, local SQLite data, caches, historical reports, and all existing worktree contents.
- Never force-push or claim frozen evaluation scores for a different checkout.
- Do not treat a local Streamlit process as a cloud deployment.
- Only stage reviewed project files; verify both remote refs after pushing.

---

### Task 1: Reconcile the working tree

**Files:** `src/nongshi/`, `tests/`, `README.md`, `STATUS.md`, `reports/`, product/task documentation, and `docs/superpowers/plans/` as shown by Git status.

- [ ] Compare every file present in `origin/main` with the working tree, including untracked files.
- [ ] Preserve the newest source, tests, reports, and screenshots; do not delete cached or private data.
- [x] Update report indexes with the 2026-10-26 interaction and save-error audit records.

### Task 2: Verify and commit

**Files:** Only reviewed files from Task 1.

- [ ] Run the full pytest suite, Ruff checks, format check, and `git diff --check` on the reconciled tree.
- [ ] Commit the reviewed source, tests, documentation, and evidence with a descriptive message.

### Task 3: Synchronize and deploy

**Files:** Git refs and the existing local Streamlit deployment only.

- [ ] Integrate the commit with `origin/main` without rewriting remote history.
- [ ] Push the resulting mainline commit to `origin/main` and `gitee/master`; verify both refs resolve to the same commit.
- [ ] Check the repository for a configured cloud deployment. If none exists, start the existing Streamlit app locally and report the local URL and deployment boundary.

### Acceptance

- The committed tree retains all prior upstream functionality and includes the verified local fixes.
- Full pytest, Ruff, formatting, and whitespace checks pass on the integrated commit.
- Remote ref hashes match after push; no credentials or local database files are included.
- The app is reachable through the supported deployment target, with its target stated precisely.
