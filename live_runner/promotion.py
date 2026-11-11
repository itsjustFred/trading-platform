"""The one enforcement function of the promotion gate (#165).

Every place that can put a deployment in front of a broker calls
:func:`enforce_promotion_gate` with the deployment and its own verdict on
whether the account is real money: live preflight, the daemon's order
preparation, and the viewer's deployment create / activate / cTrader top-N
endpoints. There is no second implementation.

The gate is an ADDITIONAL layer. The platform's existing hard block on real
money (the daemon refuses any adapter that is not provably demo/paper, the
viewer refuses to arm an account the broker does not report as demo) is
untouched and still applies to promoted code.

What is hashed and which parameters are bound
---------------------------------------------
* **Signal-bot deployment** (a ``signal_processor`` is configured): the bot's
  processor module (``signal_bots/<bot>/processor.py``, resolved through
  :mod:`signal_bots.registry`). Bound parameters are everything that
  parameterises the processor: the registered processor id (it selects the
  variant), the deployment's ``signal_processor_config`` overrides, and the
  SHA256 of the bot's ``config.yaml`` those overrides are merged into.
* **Strategy deployment** (no signal processor): ``strategies/<name>/<name>.py``
  for the deployment's ``strategy_name``. Bound parameters are the file's
  ``STRATEGY_META["parameters"]`` defaults, and only those: no live code path
  applies per-deployment strategy parameters, so the defaults are exactly what
  the strategy trades with. Deployment ``settings`` (risk limits, selection
  metadata, auth overrides, ...) do not parameterise the strategy and are not
  bound. A deployment that nevertheless carries ``settings["strategy_parameters"]``
  is refused on a real-money account: it would look like an override the gate
  checked while nothing applies it. Optimised parameters are promoted by
  writing them into ``STRATEGY_META`` (which also changes the hashed file).

Anything that cannot be resolved on a real-money account is refused: no file,
an unreadable ``STRATEGY_META``, an unknown processor, a malformed override.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from runner.approval_gate import _sha256_of
from runner.promotion_gate import StrategyNotPromotedError, require_promoted
from shared.models import LiveDeploymentConfig
from shared.strategy_registry import DEFAULT_STRATEGIES_DIR, load_strategy_meta

SIGNAL_BOTS_ROOT = Path(__file__).resolve().parent.parent / "signal_bots"
STRATEGY_PARAMETERS_KEY = "strategy_parameters"


class PromotionTargetError(ValueError):
    """What a deployment trades could not be resolved to a promotable file."""


@dataclass(frozen=True)
class PromotionTarget:
    """The file a promotion is bound to, and the parameters bound with it."""

    kind: str
    path: Path
    #: Directory the ledger's ``file`` entry is relative to.
    root: Path
    parameters: dict[str, Any]


def deployment_signal_processor_name(deployment: LiveDeploymentConfig) -> str:
    """Registered processor id of a deployment, ``""`` when none is configured."""
    configured = deployment.signal_config or {}
    settings = deployment.settings or {}
    return str(
        configured.get("signal_processor") or settings.get("signal_processor") or ""
    ).strip().lower()


def signal_bot_target(
    processor_name: str, processor_config: Mapping[str, Any] | None = None
) -> PromotionTarget:
    """Promotion target of a registered signal processor with these overrides."""
    from signal_bots.registry import (
        get_signal_processor_config_path,
        get_signal_processor_module_path,
    )

    name = str(processor_name or "").strip().lower()
    try:
        module_path = get_signal_processor_module_path(name)
        config_path = get_signal_processor_config_path(name)
    except ValueError as exc:
        raise PromotionTargetError(str(exc)) from exc
    if not module_path.is_file():
        raise PromotionTargetError(f"signal processor module not found for '{name}'")
    try:
        config_sha256 = _sha256_of(config_path)
    except OSError as exc:
        raise PromotionTargetError(
            f"signal processor config is unreadable for '{name}'"
        ) from exc
    return PromotionTarget(
        kind="signal_bot",
        path=module_path,
        root=module_path.parent.parent,
        parameters={
            "signal_processor": name,
            "signal_processor_config": dict(processor_config or {}),
            "bot_config_sha256": config_sha256,
        },
    )


def strategy_target(
    strategy_name: str,
    *,
    strategies_dir: Path | str | None = None,
) -> PromotionTarget:
    """Promotion target of ``strategies/<name>/<name>.py`` and its META defaults."""
    root = Path(strategies_dir) if strategies_dir is not None else DEFAULT_STRATEGIES_DIR
    name = str(strategy_name or "").strip()
    # The name comes from a deployment record, so it only ever selects a
    # directly named strategy directory, never a path.
    if not name or name in {".", ".."} or any(char in name for char in "/\\:\0"):
        raise PromotionTargetError(f"'{strategy_name}' is not a strategy name")
    path = root / name / f"{name}.py"
    if not path.is_file():
        # Relative on purpose: this text reaches API clients.
        raise PromotionTargetError(
            f"no strategy file for '{name}' (expected strategies/{name}/{name}.py)"
        )
    try:
        meta = load_strategy_meta(path, root)
    except Exception as exc:
        raise PromotionTargetError(f"STRATEGY_META of '{name}' is unreadable: {exc}") from exc
    defaults = meta.get("parameters", {}) if isinstance(meta, Mapping) else None
    if not meta or not isinstance(defaults, Mapping):
        # load_strategy_meta returns {} for anything it could not parse. The
        # defaults are part of what is bound, so "unknown" cannot be bound.
        raise PromotionTargetError(f"STRATEGY_META of '{name}' is missing or unreadable")
    return PromotionTarget(
        kind="strategy",
        path=path,
        root=root,
        parameters=dict(defaults),
    )


def deployment_promotion_target(
    deployment: LiveDeploymentConfig,
    *,
    strategies_dir: Path | str | None = None,
) -> PromotionTarget:
    """Resolve what ``deployment`` trades. Raises ``PromotionTargetError``."""
    processor_name = deployment_signal_processor_name(deployment)
    if processor_name:
        processor_config = (deployment.signal_config or {}).get("signal_processor_config")
        if processor_config is not None and not isinstance(processor_config, Mapping):
            raise PromotionTargetError("signal_processor_config must be a mapping")
        return signal_bot_target(processor_name, processor_config)

    if STRATEGY_PARAMETERS_KEY in (deployment.settings or {}):
        raise PromotionTargetError(
            f"settings.{STRATEGY_PARAMETERS_KEY} is not applied by the live runner; "
            "promote the parameters by writing them into STRATEGY_META"
        )
    return strategy_target(deployment.strategy_name, strategies_dir=strategies_dir)


def is_real_money(demo_flag: object) -> bool:
    """Real money unless the source of truth literally says demo/paper.

    ``None``, a missing answer or anything that is not the boolean ``True`` is
    real money: an unknown account type fails closed.
    """
    return demo_flag is not True


def enforce_promotion_gate(
    deployment: LiveDeploymentConfig,
    *,
    real_money: bool,
    strategies_dir: Path | str | None = None,
) -> None:
    """Raise ``StrategyNotPromotedError`` unless this deployment may trade.

    ``real_money`` is the caller's verdict from the same source of truth its
    existing demo-only block uses; build it with :func:`is_real_money`. Only a
    literal ``False`` (provably paper/demo) skips the gate.
    """
    if real_money is False:
        return
    label = (
        f"deployment '{deployment.strategy_name}' on real-money account "
        f"'{deployment.account_id}'"
    )
    try:
        target = deployment_promotion_target(deployment, strategies_dir=strategies_dir)
    except PromotionTargetError as exc:
        raise StrategyNotPromotedError(
            f"Promotion gate refused {label}: {exc}. Real-money accounts only "
            "accept code promoted to production."
        ) from exc
    try:
        require_promoted(
            target.path, target.parameters, kind=target.kind, root=target.root
        )
    except StrategyNotPromotedError as exc:
        raise StrategyNotPromotedError(
            f"Promotion gate refused {label}: {exc}. Real-money accounts only "
            "accept code promoted to production."
        ) from exc
