# Code Review: FinAlly AI Trading Workstation

## Summary of what changed

The working tree contains three categories of change against HEAD (commit `14550e1`):

1. **Massive deletion (~5,400 lines):** The entire `backend/` directory — all market-data implementation, tests, `pyproject.toml`, and `uv.lock` — plus both `.github/workflows/` files, `planning/MARKET_DATA_SUMMARY.md`, and all five `planning/archive/*.md` docs.
2. **Doc edits:** `planning/PLAN.md` (LLM provider strategy rewritten cerebras → OpenRouter-free + Groq fallback; plus a new Section 13 resolution log), `CLAUDE.md` (removed mention of completed market-data component), and `README.md` (reduced to two lines).
3. **New untracked files:** `.claude/agents/reviewer.md` and `.claude/commands/doc-review.md`.

There is no source code in this diff — it is documentation and agent-config changes plus a large deletion. The review below is scoped accordingly.

---

## CRITICAL

### C1. The entire committed `backend/` is deleted from the working tree — verify this is intentional
The `backend/` directory is physically empty on disk; every tracked file under it shows as `deleted` (`git status`), confirmed by listing the directory. This is real working-tree data loss, not staged metadata.

This was substantial committed work, authored across two real commits:
- `395eaa7` "feat: implement complete market data backend"
- `f89aa14` "Fix all issues from market data code review"

It includes the GBM simulator (`backend/app/market/simulator.py`, 270 lines), Massive client, price cache, SSE stream, factory/interface, and a full pytest suite (`backend/tests/market/*`, 8 test files).

It is recoverable (still in HEAD; `git restore backend/` or `git checkout -- backend/`), so nothing is permanently lost yet — but if this deletion is committed, the project loses its only implemented component. Recommendation: confirm whether wiping the backend is deliberate (e.g., a planned rebuild). If not, restore with `git restore backend/`. The same question applies to `.github/workflows/` (CI removed) and the `planning/archive/` design docs.

### C2. cerebras skill now directly contradicts PLAN.md Section 9
`planning/PLAN.md` Section 9 was rewritten to: call `openrouter/openai/gpt-oss-120b:free` first, then fall back to the **Groq llama** model on failure, and the cerebras-inference skill references were deliberately removed (see the new Section 13, item 1: "cerebras references removed").

But the tracked skill at `.claude/skills/cerebras/SKILL.md` still instructs the opposite:
- `MODEL = "openrouter/openai/gpt-oss-120b"` (no `:free` suffix)
- `EXTRA_BODY = {"provider": {"order": ["cerebras"]}}` (forces Cerebras, the provider PLAN.md removed)
- It documents no Groq fallback at all.

Any agent that follows this skill will produce LLM code that violates the current plan (wrong model id, wrong provider, missing the required Groq fallback using `GROQ_API_KEY`). Recommendation: either delete/rewrite the cerebras skill to match Section 9 (OpenRouter free model first, Groq llama fallback), or, if cerebras is actually still intended, revert the Section 9 edit. The two cannot both stand.

---

## IMPORTANT

### I1. New `reviewer` agent will silently overwrite this very review
`.claude/agents/reviewer.md` hardcodes: "You review the file planning/PLAN.md and write your feedback to planning/REVIEW.md". The `description` ("carry out comprehensive review when requested") is vague enough that the harness may auto-dispatch it. Because its output path is fixed to `planning/REVIEW.md` and it is scoped only to `PLAN.md`, repeated runs clobber prior reviews and it cannot review anything else. Recommendation: parameterize the target/output (as the sibling `doc-review` command does with `$ARGUMENTS`) or tighten the description so it is only invoked explicitly.

### I2. `.claude/agents/` and `.claude/commands/` are untracked while peers are tracked
`.claude/settings.json` and `.claude/skills/cerebras/SKILL.md` are committed, but the two new files under `.claude/agents/` and `.claude/commands/` are untracked. If these agent/command definitions are meant to be shared (the whole project premise is "agents interact through files"), they should be committed; otherwise they will not exist for any other clone/CI. Recommendation: decide and `git add` them, or move to `settings.local.json`-style local-only if personal.

### I3. README.md reduced to a 2-line stub
`README.md` went from a complete quick-start (build/run commands, env-var table, project structure) to a two-line description. CLAUDE.md says "Keep README.md concise" — but this removes the run instructions entirely, including the `docker run` command. Note the *old* README also still referenced "OpenRouter (Cerebras inference)" and the named-volume `docker run -v finally-data:...`, both now stale per PLAN.md edits — so a simple revert is wrong. Recommendation: restore a concise README that matches current PLAN.md (OpenRouter free + Groq fallback; bind-mount `-v "$(pwd)/db:/app/db"`; include `GROQ_API_KEY`).

---

## MINOR

### M1. `CLAUDE.md` no longer points to surviving docs that still exist in HEAD
The edit to `CLAUDE.md` removed the pointer to `planning/MARKET_DATA_SUMMARY.md` and `planning/archive/`. That is internally consistent *if* those files are being deleted (they are, in this same diff). No action needed beyond confirming C1 — but if the backend deletion is reverted, this CLAUDE.md edit should be reverted too, or it will hide existing docs.

### M2. PLAN.md missing trailing newline
`CLAUDE.md` line ends with `@planning/PLAN.md` and `\ No newline at end of file`. Pre-existing and harmless, but worth a trailing newline for POSIX-tool friendliness.

### M3. Section 13 resolution log duplicates content already folded into the doc
PLAN.md's new Section 13 restates decisions "already folded into the sections above" by its own admission. This is fine as a changelog, but it is the kind of redundancy the project's own `doc-review` command flags. Consider moving it to `planning/archive/` once stable so PLAN.md stays the single lean contract. Low priority.

---

## Positive notes
- The PLAN.md substantive edits are internally consistent and resolve real prior contradictions: the previous-close % baseline (Section 6), the Docker bind-mount aligning Section 11 with Section 4, the `GET /api/chat/history` addition (Sections 8/10), and the SSE connection-state derivation (Section 10) are all coherent and well-specified.
- The only remaining cross-document contradiction introduced by these edits is C2 (the cerebras skill), which is the one thing to fix before any LLM code is written.

## Key files referenced
- `backend/` (deleted — recoverable from HEAD)
- `.claude/skills/cerebras/SKILL.md` (contradicts PLAN.md Section 9)
- `.claude/agents/reviewer.md` (untracked; fixed output path)
- `.claude/commands/doc-review.md` (untracked)
- `planning/PLAN.md` (edited; new Section 13)
- `README.md` (reduced to stub; now partly stale vs PLAN)
- `CLAUDE.md` (edited)
