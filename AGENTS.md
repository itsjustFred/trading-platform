# AGENTS.md

This repo executes live trades as well as critical backtests and has a very high standard for correctness, reliability, clarity, maintainability and testing.
Keep scripts and paths portable across Linux, Windows and macOS.
When creating an issue or PR, mention model as well as harness at the bottom of its body. If using t3code, include that as well, but don't leave out original harness.
Unless explicitly told, never push directly to main.
Use conventional commit style messages unless not appropriate.
If making changes, also check if you need to update other things such as docs, skills, etc.

## Tests

Do NOT weaken, remove, bypass or rewrite a test just to achieve a passing result, instead fix the underlying problem. Change tests only when the task changes or you can independently identify that the test is wrong.
Install the dev tooling with `uv sync --frozen --extra dev` and run `uv run ruff check <changed files>` and `uv run mypy` (no arguments) before a PR.
The test sandbox (see `tests/conftest.py`) guards only the pytest process: a test that starts a Python subprocess must give it a sandboxed environment explicitly, or a child importing `shared.constants` loads the checkout's real `.env`.

## Issues

When creating an issue, give the appropriate labels. If things are unclear, give the `needs-decision` label. This signals that a maintainer needs to look at the issue and decide on unclear points, like open questions.

## Pull Requests

Before starting work, check open PRs for an existing PR on this issue, and do not submit a competing one unless explicitly told by the user, instead add useful context or an alternative approach.
Before creating a PR, make sure to have a complete, review-ready change. This includes running all relevant tests making sure none fails. You don't need to run the full test suite locally, but at least the tests for the code you've changed and code that uses it. If a required check cannot run, report the limitation and do not claim the change is ready. Strategy tests that need real market data skip unless `TP_TEST_OHLCV_DIR` points at an OHLCV directory; a skip is not a pass.
Include test coverage for the change.
Treat the project CI as a confirmation of the locally validated change, do not rely on an open PR as the primary development loop.
After you're done with the work and have added it to the PR, run the skill in `.agents/skills/platform-pr-verify` as a verification pipeline, unless instructed otherwise.
When your work on a PR is done and a human needs to review it because an agent's review is not enough (e.g. a behaviour change on the live-trading path, a judgement call the issue leaves open, or something a human directly touches like the frontend), add the `ready-for-human` label and say in a comment what the human should look at.
When referencing an issue, include it in the PR description, but not in the title.
After merging a PR that fully solves an issue, close the issue unless told otherwise. If it only partly solves it, leave it open and comment remaining scope with PR link.
When being asked whether you are confident to merge a PR, also look at the checks and read the comment for accepted findings and check that `pr-verify-pass` is set and the SHA in the `<!-- platform-pr-verify -->` comment equals the current head; a label left over from an older commit is not a pass. If you think these aren't ok, say so. When giving reasons why you are not confident, list all of them. If you are confident and a `<model>-confident` label exists for your model (e.g. `opus-confident`), add it.
When merging a PR, use squash-merge and set the commit message to use the format `<type>(<scope>): <description> (#<number>)`, e.g. `fix(optimizer): annualise Sharpe at measured observation density (#392)`.
