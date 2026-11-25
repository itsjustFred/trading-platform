---
name: platform-pr-verify
description: Explains what to verify in a PR to get it to `pr-verify-pass`. Use after every push to a PR, skip on PRs with the `strategy-integration` label.
---

The repo has multiple PR review bots and checks running for PRs:

- SonarCloud: static analysis. You can also list all issues with `sonar list issues --project itsjustFred_trading-platform --pull-request <N> --format toon`. With many issues the output gets large (e.g. PR #139 about 190k characters), so filter with `--severities` or redirect to a file and search it instead of reading it whole.
- `mutation-testing` action: Agent that mutates the PR's changed code and looks for surviving mutations. Add missing tests for surviving mutations.
- `pre-mortem` action: Agent writing grounded future-failure scenarios for changed code. Fix valid findings without expanding scope.
- `code-review` action: Agent reviewing the code for errors first, then clarity, then spec and repo standards. Fix valid findings.
- cubic: AI-powered code review bot. Fix valid findings.

If you want a second AI-powered review bot, trigger CodeRabbit manually. Note limits: 3 reviews/h, max. 150 files/review.

While these tools help provide valuable insights, they can be wrong, and the agent actions usually run on weaker models than you. Accept a finding only with a concrete reason. Record the result in one PR comment that starts with `<!-- platform-pr-verify -->`: the head commit SHA you verified and every accepted finding across all tools, each with a one-line reason, or "No accepted findings". Edit this comment when findings change instead of adding a new one.

When every check on the head commit has finished and is green, or each of its findings is fixed or accepted, add the `pr-verify-pass` label.
`pr-verify-pass` is valid only for the commit it was given on. Before you push a new commit to the PR, remove the label; after the push, run this skill again.
