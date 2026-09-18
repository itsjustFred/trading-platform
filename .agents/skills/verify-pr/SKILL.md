---
name: verify-pr
description: Mandatory verification pipeline for a pull request. Runs SonarQube comment check, mutation testing, pre-mortem, code review and CodeRabbit in sequence until all pass without findings.
---

Use this guide as the verification pipeline for the PR.

If needed programs/skills are not installed, take from here:
- https://docs.coderabbit.ai/cli
- https://github.com/honnibal/claude-skills/blob/main/mutation-testing.md.txt
- https://github.com/honnibal/claude-skills/blob/main/pre-mortem.md.txt
- https://github.com/mattpocock/skills/tree/main/skills/engineering/code-review

Run the following review pipeline step-by-step. If a step fails, fix the issue first, then re-run that step; only after it passes, go to the next step.

1. Check PR comments for SonarQube. Make sure that the quality gate passes or the failure reason is acceptable (e.g. Cognitive Complexity too high on unimportant functions, or rules explicitly accepted by the user).
2. Run the `mutation-testing` skill, without asking questions to the user. The scope is the PR's changed files. A survived mutation means a missing test case: add a test in the PR, re-run until all mutations are killed. Only run on the first review cycle; in following cycles only verify mutation kill via tests.
3. Run the `pre-mortem` skill with scope limited to changed files, without asking user questions, hardening without scope extension.
4. Run the `code-review` skill (by Matt Pocock). If useful, run the standards- and spec-axis via subagents. The fixed point is `main`.
5. Run the `coderabbit` CLI: info/trivial findings are allowed (on the free tier there is a limit of 150 files/review and 3 reviews/h; if rate-limited, the remaining time is shown).

The loop is only done when all steps complete without findings.
