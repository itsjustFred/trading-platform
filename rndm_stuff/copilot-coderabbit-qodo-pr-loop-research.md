# Research: Copilot CLI → PR → CodeRabbit → Fix-Loop → Qodo (manuell) → Merge

## Executive Summary

Ja, der gewünschte Loop ist **grundsätzlich umsetzbar**. GitHub kann den Merge über Branch Protection, Required Checks und optional Auto-Merge steuern, sodass PRs erst bei erfüllten Bedingungen gemerged werden.[^1][^2][^3][^4]  
CodeRabbit lässt sich automatisch oder manuell triggern; bei Fehlern kannst du iterativ fixen und per `@coderabbitai review` erneut prüfen lassen.[^5][^6]  
Qodo lässt sich **gezielt manuell** per PR-Kommentar `/agentic_review` starten, während Auto-Review über `.pr_agent.toml` steuerbar ist.[^11][^12]  
Der kritischste Punkt ist das **Free-Tier-Modell**: Bei CodeRabbit sind die Limits im Free-Plan klar begrenzt, und „free forever“ wird explizit für Public/OSS kommuniziert; für Private Repos ist mindestens Trial/Plan-Details genau zu prüfen.[^7][^8]

## Prerequisites

1. GitHub-Repo mit aktivem PR-Flow und Branch Protection (mindestens Required Reviews + Required Status Checks).[^1][^2]  
2. CodeRabbit in dem Repo installiert und konfiguriert (`.coderabbit.yaml`), inkl. gewünschter Auto/Manual-Strategie.[^5]  
3. Qodo GitHub-Integration aktiv + `.pr_agent.toml` im Repo, damit man Auto-Verhalten steuern und manuelle Commands nutzen kann.[^11][^12]  
4. Falls du private Repos hast: GitHub-Plan prüfen, weil Branch Protection/Auto-Merge in Private Repos nicht auf jedem Free-Plan verfügbar ist.[^1][^3][^4]

## Empfohlener Loop (praktisch umsetzbar)

1. **Copilot CLI codet**, du pushst Branch und erstellst PR.  
2. **CodeRabbit prüft** (auto oder manuell). Bei Findings: fixen, pushen, wiederholen. Für on-demand Re-Run: `@coderabbitai review` (inkrementell) oder `@coderabbitai full review` (komplett).[^5][^6]  
3. Wenn CodeRabbit + CI „clean“ sind, **Qodo manuell triggern** mit `/agentic_review`.[^11][^12]  
4. Bei Qodo-Findings: fixen, pushen, wieder `/agentic_review` (oder optional `/checks` für CI-Fehleranalyse).[^11][^12][^14]  
5. **Merge nur bei erfüllten Gates**: Required Checks grün, Required Reviews erfüllt, offene Diskussionen gelöst; optional Auto-Merge einschalten.[^1][^2][^3][^4]

## Konfigurationsideen

### 1) CodeRabbit: auto an, aber kontrollierbar

CodeRabbit reviewed standardmäßig automatisch; du kannst aber Opt-in/Filter/Branches steuern oder Auto komplett ausmachen und nur manuell triggern.[^5]

```yaml
# .coderabbit.yaml
reviews:
  auto_review:
    enabled: true
    auto_incremental_review: true
    drafts: false
    # optional: nur bestimmte Ziel-Branches
    base_branches:
      - "main"
```

Wenn du „semi-manual“ willst:

```yaml
reviews:
  auto_review:
    enabled: false
```

Dann PR-weise triggern mit `@coderabbitai review`.[^5][^6]

### 2) Qodo: Auto-Review aus, manuell per Command

Qodo kann automatisch über `pr_commands` laufen oder manuell per `/agentic_review`.[^11][^12]

```toml
# .pr_agent.toml (GitHub)
[github_app]
# Auto nur Summary (optional) – KEIN auto review:
pr_commands = ["/agentic_describe"]
handle_push_trigger = false
```

Manueller Review-Schritt im PR:

```text
/agentic_review
```

[^11][^12][^13]

### 3) GitHub Merge-Gates (zentral)

Für deinen Zielprozess sind diese Branch-Regeln wichtig:

- Require pull request reviews
- Require status checks to pass
- Require conversation resolution
- optional: Require branch up-to-date (strict checks)
- optional: Auto-Merge aktivieren

[^1][^2][^3][^4]

## Was in Free-Tiers kritisch ist

| Thema | Belastbare Info | Wirkung auf deinen Loop |
|---|---|---|
| CodeRabbit Free-Limits | Free-Plan hat per-Developer-Limits; PR-Reviews sind begrenzt (`3 summary/hour`) und Files/Review sind limitiert.[^7] | Bei häufigen Iterationen kann der Loop gedrosselt werden. |
| CodeRabbit „free forever“ | Öffentlich kommuniziert als free forever für **public repositories** (OSS).[^8] | Für private Repos nicht als „free forever full review“ eindeutig garantiert. |
| CodeRabbit Private Repo Support | Quickstart nennt public/private Repo-Integration.[^9] | Technisch möglich, aber Tier-/Leistungsgrenzen prüfen. |
| Qodo Free | Qodo-Doku nennt Free/Team/Enterprise; Pricing nennt promotions/quoten (z. B. PR- oder Credit-Limits, variabel).[^15][^16] | Manual-Trigger-Strategie passt gut, aber Quoten können limitieren. |
| GitHub Private Repo Controls | Branch Protection + Auto-Merge in Private Repos hängen vom GitHub-Plan ab.[^1][^3][^4] | Ohne passenden Plan bleibt Merge teils manuell. |

> Hinweis: In der Tabelle ist bei CodeRabbit/Qodo absichtlich konservativ formuliert, weil Marketing-/Pricing-Seiten sich über Zeit schnell ändern können.

## Konkrete Umsetzungsidee (robust)

1. **Gate-Definition**: „mergeable“ = CI grün + 0 offene High-Severity Findings + required reviewer approval.  
2. **CodeRabbit zuerst**: auto aktiv lassen, iterative Fixes, bei Bedarf `@coderabbitai review`.  
3. **Qodo danach manuell**: `/agentic_review` als gezielter zweiter Blick nur auf reife PRs.  
4. **Merge-Entscheidung**: branch protection erzwingt objektive Gates; optional Auto-Merge für Wartezeit-Reduktion.[^1][^2][^3]  

Wenn du diesen Loop später weiter automatisieren willst, kannst du zusätzlich über den GitHub `issue_comment`-Event Workflows starten, die auf PR-Kommandos reagieren.[^17]

## Confidence Assessment

- **Hohe Sicherheit**: Technische Machbarkeit des Loops, Command-Namen, Auto/Manual-Steuerung in CodeRabbit und Qodo, GitHub-Gates (Branch Protection, Status Checks, Auto-Merge).[^1][^2][^3][^4][^5][^6][^11][^12]  
- **Mittlere Sicherheit**: Exakte Free-Tier-Grenzen über längere Zeit (insb. Promotions bei Qodo, Plan-Details/Positionierung bei CodeRabbit), da Pricing-Seiten sich häufig ändern.[^7][^8][^15][^16]  
- **Abhängigkeit von deinem Setup**: Ob dein privates Repo den vollen Gate-Automatismus nutzen kann, hängt am GitHub-Plan/Org-Kontext.[^1][^3][^4]

## Footnotes

[^1]: GitHub Docs, **About protected branches** (Verfügbarkeit nach Plan, Branch-Protection-Optionen inkl. required reviews/checks/conversation resolution): https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/about-protected-branches  
[^2]: GitHub Docs, **About status checks** (Required checks blocken Merge bis „pass“): https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/collaborating-on-repositories-with-code-quality-features/about-status-checks  
[^3]: GitHub Docs, **Automatically merging a pull request** (Auto-Merge nur nach erfüllten Merge-Requirements): https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/incorporating-changes-from-a-pull-request/automatically-merging-a-pull-request  
[^4]: GitHub Docs, **Managing auto-merge for pull requests in your repository** (Repo-Setting + Plan-Verfügbarkeit): https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/configuring-pull-request-merges/managing-auto-merge-for-pull-requests-in-your-repository  
[^5]: CodeRabbit Docs, **Auto-review configuration** (`reviews.auto_review.*`, default enabled, manual trigger fallback): https://docs.coderabbit.ai/configuration/auto-review  
[^6]: CodeRabbit Docs, **Review commands** (`@coderabbitai review`, `@coderabbitai full review`, pause/resume): https://docs.coderabbit.ai/reference/review-commands  
[^7]: CodeRabbit Docs, **Plans and pricing** (Free/Trial/OSS/Pro Limits, per-developer hourly rate limits): https://docs.coderabbit.ai/management/plans  
[^8]: CodeRabbit Pricing, FAQ/Plan-Kommunikation (u. a. „free reviews forever for public repositories“, 14-day trial): https://www.coderabbit.ai/pricing  
[^9]: CodeRabbit Docs, **Quickstart** (GitHub public/private repository setup): https://docs.coderabbit.ai/getting-started/quickstart  
[^10]: CodeRabbit Docs, **GitHub Checks tool** (CI-Checks einbeziehen, Timeout, manueller re-review command): https://docs.coderabbit.ai/tools/github-checks  
[^11]: Qodo Docs, **Code review in PRs** (manual `/agentic_review`, automatic review option): https://docs.qodo.ai/code-review/get-started/use-qodo-in-prs/code-review  
[^12]: Qodo Docs, **Configuration and command reference** (`pr_commands`, `/agentic_review`, `/checks`, push-trigger behavior): https://docs.qodo.ai/code-review/get-started/configuration-overview/configuration-and-command-reference  
[^13]: Qodo Docs, **Persistent review comments** (`handle_push_trigger`, `push_commands`): https://docs.qodo.ai/code-review/get-started/use-qodo-in-prs/code-review/persistent-review-comments  
[^14]: Qodo Docs, **Enable CI failure feedback** (`/checks`, auto checks feedback config): https://docs.qodo.ai/code-review/concepts/enable-ci-failure-feedback  
[^15]: Qodo Docs, **Subscription plans** (Free/Team/Enterprise overview): https://docs.qodo.ai/subscription-plans  
[^16]: Qodo Pricing (public pricing/promotions/credits details; can vary over time): https://www.qodo.ai/pricing/  
[^17]: GitHub Docs, **Events that trigger workflows – issue_comment** (PR comments können Workflows triggern): https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#issue_comment
