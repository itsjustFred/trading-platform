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
  :mod:`signal_bots.registry`) is the promoted file. Every repository module
  the processor imports, directly or through a package ``__init__`` it
  executes, is bound as well (see :func:`repository_import_closure`): a bot
  that imports another bot's processor, or a shared model, trades that code
  too. Bound parameters are everything that parameterises the processor: the
  registered processor id (it selects the variant), the deployment's
  ``signal_processor_config`` overrides, the SHA256 of the bot's
  ``config.yaml`` those overrides are merged into, and the SHA256 of each
  imported repository module.
* **Strategy deployment** (no signal processor): ``strategies/<name>/<name>.py``
  for the deployment's ``strategy_name``. Bound parameters are the file's
  ``STRATEGY_META["parameters"]`` defaults, and only those: no live code path
  applies per-deployment strategy parameters, so the defaults are exactly what
  the strategy trades with. Deployment ``settings`` (risk limits, selection
  metadata, auth overrides, ...) do not parameterise the strategy and are not
  bound. Any deployment, strategy or signal bot, that carries
  ``settings["strategy_parameters"]`` is refused on a real-money account: it
  would look like an override the gate checked while nothing applies it.
  Optimised parameters are promoted by writing them into ``STRATEGY_META``
  (which also changes the hashed file).

Anything that cannot be resolved on a real-money account is refused: no file,
an unreadable ``STRATEGY_META``, an unknown processor, a malformed override.
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from runner.approval_gate import _sha256_of
from runner.promotion_gate import StrategyNotPromotedError, require_promoted
from shared.models import LiveDeploymentConfig
from shared.strategy_registry import DEFAULT_STRATEGIES_DIR, load_strategy_meta

SIGNAL_BOTS_ROOT = Path(__file__).resolve().parent.parent / "signal_bots"
#: Repository root that the import closure of a signal bot is resolved against.
REPOSITORY_ROOT = SIGNAL_BOTS_ROOT.parent
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
    """Registered processor id of a deployment, ``""`` when none is configured.

    Delegates to the model's own property: deployment validation (#150 trailing
    gates) and the daemon resolve the same id through the same definition, so
    the promotion gate hashes the processor that is actually applied there.
    """
    return deployment.signal_processor_name


def _module_file(name: str, repo_root: Path) -> Path | None:
    """The repository file that defines dotted module ``name``, if there is one."""
    parts = name.split(".")
    if not all(parts):
        return None
    for candidate in (
        repo_root.joinpath(*parts).with_suffix(".py"),
        repo_root.joinpath(*parts, "__init__.py"),
    ):
        if candidate.is_file():
            return candidate
    return None


def _parent_names(dotted: str) -> list[str]:
    """``a.b.c`` -> ``["a", "a.b", "a.b.c"]``: every package Python executes to load it."""
    parts = dotted.split(".")
    return [".".join(parts[: index + 1]) for index in range(len(parts))]


#: One import statement: (level, module, imported names, is_from_import).
_ImportStatement = tuple[int, str, tuple[str, ...], bool]
#: Parsed import statements keyed by the SHA256 of the source they came from.
#: Imports depend only on the content, so a changed file is a new key.
_IMPORTS_BY_DIGEST: dict[str, tuple[_ImportStatement, ...]] = {}
_IMPORTS_CACHE_LIMIT = 1024


def _import_statements(source: bytes, digest: str, filename: str) -> tuple[_ImportStatement, ...]:
    cached = _IMPORTS_BY_DIGEST.get(digest)
    if cached is not None:
        return cached
    tree = ast.parse(source.decode("utf-8"), filename=filename)
    statements: list[_ImportStatement] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            statements.append((0, "", tuple(alias.name for alias in node.names), False))
        elif isinstance(node, ast.ImportFrom):
            statements.append(
                (
                    node.level,
                    node.module or "",
                    tuple(alias.name for alias in node.names),
                    True,
                )
            )
    if len(_IMPORTS_BY_DIGEST) >= _IMPORTS_CACHE_LIMIT:
        _IMPORTS_BY_DIGEST.clear()
    result = tuple(statements)
    _IMPORTS_BY_DIGEST[digest] = result
    return result


def _imported_names(statement: _ImportStatement, *, package: str | None) -> set[str]:
    """Dotted names one import statement may load. ``package`` is None outside the repo."""
    level, module, names, is_from = statement
    if not is_from:
        return set(names)
    if level:
        if package is None:
            raise PromotionTargetError("a relative import outside the repository cannot be bound")
        parts = package.split(".") if package else []
        if level - 1 > len(parts):
            raise PromotionTargetError("a relative import reaches above the repository root")
        base_parts = parts[: len(parts) - (level - 1)]
    else:
        base_parts = []
    base = ".".join([*base_parts, *([module] if module else [])])
    result = {base} if base else set()
    for name in names:
        if name != "*":
            result.add(".".join(part for part in (base, name) if part))
    return result


def repository_import_closure(start: Path, *, repo_root: Path | None = None) -> dict[str, str]:
    """SHA256 of every repository module that importing ``start`` executes.

    Walks the static ``import`` / ``from ... import`` statements of ``start``
    and of each repository module it reaches. A dotted name binds every file
    Python would execute to load it: the module itself and each parent package
    ``__init__.py``; the parent packages of ``start`` itself are seeded the
    same way. Only files under ``repo_root`` are returned, keyed by their
    POSIX path relative to it. ``start`` itself is not included: the promoted
    file is bound by its own SHA256. Standard-library and third-party names
    resolve to nothing under the repository and are skipped.

    Dynamic imports (``importlib``, ``__import__``) are not visible to this walk.
    """
    root = (repo_root or REPOSITORY_ROOT).resolve()
    start_resolved = start.resolve()
    seen: set[Path] = {start_resolved}
    todo: list[Path] = [start_resolved]
    found: dict[str, str] = {}
    resolved_names: dict[str, Path | None] = {}
    if start_resolved.is_relative_to(root):
        # Importing start executes every parent package __init__.py first.
        package_dirs = start_resolved.relative_to(root).parent.parts
        for depth in range(1, len(package_dirs) + 1):
            init = root.joinpath(*package_dirs[:depth], "__init__.py")
            if init.is_file() and init.resolve() not in seen:
                resolved = init.resolve()
                seen.add(resolved)
                todo.append(resolved)
                found[_repository_key(resolved, root)] = _sha256_of(resolved)
    while todo:
        current = todo.pop()
        try:
            source = current.read_bytes()
            statements = _import_statements(
                source, hashlib.sha256(source).hexdigest(), str(current)
            )
        except (OSError, UnicodeError, SyntaxError) as exc:
            raise PromotionTargetError(
                f"imports of {current.name} cannot be read for promotion: {exc}"
            ) from exc
        package: str | None = None
        if current.is_relative_to(root):
            # The package a module's relative imports resolve against.
            package = ".".join(current.relative_to(root).parent.parts)
        for statement in statements:
            for name in _imported_names(statement, package=package):
                for dotted in _parent_names(name):
                    if dotted not in resolved_names:
                        module = _module_file(dotted, root)
                        resolved_names[dotted] = module.resolve() if module else None
                    resolved_module = resolved_names[dotted]
                    if resolved_module is None or resolved_module in seen:
                        continue
                    seen.add(resolved_module)
                    todo.append(resolved_module)
                    found[_repository_key(resolved_module, root)] = _sha256_of(resolved_module)
    return dict(sorted(found.items()))


def _repository_key(resolved: Path, root: Path) -> str:
    """POSIX path of ``resolved`` relative to the repository, or refuse the promotion.

    A module that resolves outside the repository (e.g. a symlink to an external file)
    cannot be bound by the repository's hashes, so it is refused like any other
    unpromotable target instead of escaping as a bare ``ValueError``.
    """
    if not resolved.is_relative_to(root):
        raise PromotionTargetError(
            f"{resolved} resolves outside the repository and cannot be bound for promotion"
        )
    return resolved.relative_to(root).as_posix()


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
            "imported_modules_sha256": repository_import_closure(module_path),
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
    except Exception as exc:  # except-kind: money-decision
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
    if STRATEGY_PARAMETERS_KEY in (deployment.settings or {}):
        # Nothing applies this override, for a signal bot or a strategy alike.
        raise PromotionTargetError(
            f"settings.{STRATEGY_PARAMETERS_KEY} is not applied by the live runner; "
            "promote the parameters by writing them into STRATEGY_META"
        )
    processor_name = deployment_signal_processor_name(deployment)
    if processor_name:
        processor_config = (deployment.signal_config or {}).get("signal_processor_config")
        if processor_config is not None and not isinstance(processor_config, Mapping):
            raise PromotionTargetError("signal_processor_config must be a mapping")
        return signal_bot_target(processor_name, processor_config)
    if (
        str(deployment.signal_source or "internal").lower() != "internal"
        or str(deployment.signal_source_type or "internal").lower() != "internal"
    ):
        # Without a processor the daemon passes the raw channel signal through;
        # no strategy file runs, so a strategy of the same name proves nothing.
        raise PromotionTargetError(
            "a non-internal deployment without a signal_processor trades raw "
            "channel signals, which cannot be promoted"
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
