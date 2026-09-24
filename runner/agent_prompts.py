"""LLM prompt builders for the auto strategy-conversion agent.

All step prompts (analyst/architect/coder) live here so the orchestration
module stays free of prompt text. Prompts are intentionally German — they
instruct the LLM and are part of the generated-artifact contract.
"""

from __future__ import annotations

ERROR_CONTEXT_LIMIT = 8_000


def step_one_developer_prompt(*, requested_engine: str | None) -> str:
    engine_line = (
        f"Wenn ein Framework explizit vorgegeben wurde, dokumentiere es in '## Framework-Empfehlung' als {requested_engine}."
        if requested_engine
        else "Füge einen Abschnitt '## Framework-Empfehlung' mit genau einem Framework aus vectorbt, pynecore oder lean hinzu."
    )
    return (
        "Du bist The Analyst. Antworte ausschliesslich mit Markdown, ohne Erklaerungen ausserhalb der Dokumentation. "
        "Dokumentiere die Strategie strukturiert fuer dieses Repository. "
        "Die Doku muss mindestens folgende Abschnitte enthalten: '# <Name>', '## Quelle', '## Kernlogik', "
        "'### Entry', '### Exit', '### Stop-Loss', optional '### Trailing Stop'/'### Partial Exits', "
        "'## Indikator-Einstellungen', '## Risk-Management', '## Framework-Empfehlung'. "
        f"{engine_line}"
    )


def step_one_user_prompt(*, source_code: str, strategy_name: str | None) -> str:
    name_line = f"Bevorzugter Strategie-Name: {strategy_name}\n" if strategy_name else ""
    return (
        "Analysiere diesen Trading-Code. Extrahiere Entry/Exit-Logik, Indikatoren und Risk-Management. "
        "Antworte AUSSCHLIESSLICH mit einer strukturierten Markdown-Dokumentation.\n"
        f"{name_line}"
        "Quellcode:\n"
        "```text\n"
        f"{source_code}\n"
        "```"
    )


def step_two_developer_prompt() -> str:
    return (
        "Du bist The Architect. Antworte ausschliesslich mit genau einer Python-Zuweisung 'STRATEGY_META = {...}'. "
        "Die Antwort darf keine weiteren Erklaerungen enthalten. Das Dictionary muss mindestens die Schluessel "
        "name, version, parameters und tags enthalten. In parameters muessen alle aus dem Quellcode ableitbaren "
        "tunebaren Parameter mit sinnvollen Default-Werten enthalten sein."
    )


def step_two_user_prompt(
    *,
    source_code: str,
    markdown_doc: str,
    strategy_name: str,
) -> str:
    return (
        "Basierend auf der Doku: Generiere ausschliesslich das Python `STRATEGY_META` Dictionary fuer diese Strategie, "
        "inklusive aller benoetigten Parameter in `parameters`.\n"
        f"Strategie-Slug: {strategy_name}\n\n"
        "Markdown-Dokumentation:\n"
        "```markdown\n"
        f"{markdown_doc}\n"
        "```\n\n"
        "Original-Quellcode:\n"
        "```text\n"
        f"{source_code}\n"
        "```"
    )


def step_three_developer_prompt(*, system_prompt: str, engine: str) -> str:
    engine_guidance = (
        "Fuer vectorbt muss generate_signals(df, params) mindestens (entries, exits, indicators_dict) "
        "und optional als viertes Element drawings zurueckgeben. "
        "Alle logikrelevanten Entry-/Exit-/Stop-/Trailing-Indikatoren muessen in indicators_dict landen."
        if engine == "vectorbt"
        else (
            "Halte dich strikt an den Contract des gewaehlten Frameworks. Wenn pynecore genutzt wird, liefere bei Bedarf "
            "zusaetzlich build_trade_context(df, params) und optional build_drawings(df, params, trades, symbol), "
            "damit Trade-Meta und Chart-Drawings angereichert werden koennen."
        )
    )
    return (
        f"{system_prompt}\n\n"
        "Du bist The Coder. Antworte ausschliesslich mit Python-Code oder einem ```python```-Block. "
        "Keine Markdown-Erklaerungen. STRATEGY_META wird separat injiziert; definiere STRATEGY_META NICHT erneut. "
        "Nutze nur Bibliotheken, die in diesem Repository bereits installiert sind. "
        f"Gewaehltes Framework: {engine}. {engine_guidance}"
    )


def step_three_user_prompt(
    *,
    source_code: str,
    markdown_doc: str,
    meta_source: str,
    engine: str,
    attempt_error: str | None,
) -> str:
    repair_section = ""
    if attempt_error:
        repair_section = (
            "\n\nDer vorherige Screening-Versuch ist mit folgendem Fehler fehlgeschlagen. "
            "Korrigiere NUR den Python-Code. Markdown und STRATEGY_META bleiben unveraendert.\n"
            "```text\n"
            f"{attempt_error[-ERROR_CONTEXT_LIMIT:]}\n"
            "```"
        )
    return (
        "Schreibe nun den vollstaendigen Python Code (mit imports). Die Hauptfunktion MUSS `entries, exits, indicators_dict` "
        "und optional `drawings` als viertes Rueckgabe-Element liefern, wenn das Framework vectorbt ist. "
        "Halte dich strikt an die Logik im Markdown.\n"
        f"Framework: {engine}\n\n"
        "Verwende dieses STRATEGY_META unveraendert:\n"
        "```python\n"
        f"{meta_source}\n"
        "```\n\n"
        "Markdown Ground Truth:\n"
        "```markdown\n"
        f"{markdown_doc}\n"
        "```\n\n"
        "Original-Quellcode:\n"
        "```text\n"
        f"{source_code}\n"
        "```"
        f"{repair_section}"
    )
