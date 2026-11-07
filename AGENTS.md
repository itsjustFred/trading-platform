# AGENTS.md

This repo executes live trades as well as critical backtests and has a very high standard for correctness, reliability, clarity, maintainability and testing.
Keep scripts and paths portable across Linux, Windows and macOS.
When creating an issue or PR, mention model as well as harness at the bottom of its body. If using t3code, include that as well, but don't leave out original harness.
Unless explicitly told, never push directly to main.
Use conventional commit style messages unless not appropriate.
If making changes, also check if you need to update other things as docs, skills, etc.

## Tests

Do NOT weaken, remove, bypass or rewrite a test just to achieve a passing result, instead fix the underlying problem. Change tests only when the task changes or you can independently identify that the test is wrong.

## Issues

When creating an issue, give the appropriate labels. If things are unclear, give the `needs-decision` label. This signals that a maintainer needs to look at the issue and decide on unclear points, like open questions.

## Pull Requests

Before creating a PR, make sure to have a complete, review-ready change. This includes running all relevant tests making sure none fails. If a required check cannot run, report the limitation and do not claim the change is ready.
Include test coverage for the change.
Treat the project CI as a confirmation of the locally validated change, do not rely on an open PR as the primary development loop.
After you're done with the work and have added it to the PR, run the `platform-pr-verify` skill found in `.agents/skills/` in this repo as a verification pipeline, unless instructed otherwise.
When referencing an issue, include it in the PR description, but not in the title.
Before starting work, check open PRs for an existing PR on this issue, and do not submit a competing one unless explicitly told by the user, instead add useful context or an alternative approach.
Give the `ready-for-human` tag only if the implementation is done. If further work is required after human review, remove the tag while working and re-add when done.
After merging a PR that fully solves an issue, close the issue unless told otherwise. If it only partly solves it, leave it open and comment remaining scope with PR link.
