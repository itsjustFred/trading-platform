"""Engine seam for StrategyIR code generation routing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from runner.strategy_ir.models import StrategyIR

StrategyIRCodeGenerator = Callable[[StrategyIR], str]
IR_ROLLOUT_DEFAULT = "ir-first"
IR_ROLLOUT_MODES = frozenset({"legacy", "ir-first", "ir-only"})


def _normalize_rollout_mode(rollout_mode: str | None) -> str:
    normalized = (rollout_mode or "").strip().lower()
    if normalized in {"", "default"}:
        return IR_ROLLOUT_DEFAULT
    if normalized in {"legacy-only", "legacy_llm", "llm-only"}:
        return "legacy"
    if normalized in {"ir_first", "deterministic"}:
        return "ir-first"
    if normalized in {"ir_only", "deterministic-only", "strict-ir"}:
        return "ir-only"
    if normalized in IR_ROLLOUT_MODES:
        return normalized
    raise ValueError(
        f"Unsupported IR rollout mode: {rollout_mode!r}. "
        f"Expected one of {sorted(IR_ROLLOUT_MODES)} or a recognized alias."
    )


@dataclass(frozen=True)
class CodegenRoute:
    """Resolved route for code generation after StrategyIR build."""

    use_ir_generator: bool
    first_llm_attempt: int
    reason: str
    rollout_mode: str = IR_ROLLOUT_DEFAULT
    allow_legacy_fallback: bool = True
    generator_id: str | None = None
    generator: StrategyIRCodeGenerator | None = None


def resolve_codegen_route(
    *,
    engine: str,
    strategy_ir: StrategyIR | None,
    generators: Mapping[str, StrategyIRCodeGenerator],
    rollout_mode: str = IR_ROLLOUT_DEFAULT,
) -> CodegenRoute:
    """Resolve whether to use an IR generator or legacy LLM codegen."""
    normalized_engine = (engine or "").strip().lower()
    normalized_rollout_mode = _normalize_rollout_mode(rollout_mode)

    if normalized_rollout_mode == "legacy":
        return CodegenRoute(
            use_ir_generator=False,
            first_llm_attempt=1,
            reason="Rollout mode 'legacy' forces legacy LLM codegen.",
            rollout_mode=normalized_rollout_mode,
            allow_legacy_fallback=True,
        )

    allow_legacy_fallback = normalized_rollout_mode != "ir-only"

    if strategy_ir is None:
        return CodegenRoute(
            use_ir_generator=False,
            first_llm_attempt=1,
            reason="StrategyIR is unavailable.",
            rollout_mode=normalized_rollout_mode,
            allow_legacy_fallback=allow_legacy_fallback,
        )

    generator = generators.get(normalized_engine)
    if generator is None:
        return CodegenRoute(
            use_ir_generator=False,
            first_llm_attempt=1,
            reason=f"No IR generator is registered for engine '{normalized_engine}'.",
            rollout_mode=normalized_rollout_mode,
            allow_legacy_fallback=allow_legacy_fallback,
        )

    reason = f"Using IR generator for engine '{normalized_engine}'."
    if normalized_rollout_mode == "ir-only":
        reason += " Legacy fallback disabled by rollout mode."

    return CodegenRoute(
        use_ir_generator=True,
        first_llm_attempt=2,
        reason=reason,
        rollout_mode=normalized_rollout_mode,
        allow_legacy_fallback=allow_legacy_fallback,
        generator_id=normalized_engine,
        generator=generator,
    )
