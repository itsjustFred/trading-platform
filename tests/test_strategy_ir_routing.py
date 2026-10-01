from __future__ import annotations

import json

import pytest

from runner.strategy_ir.models import (
    EngineHint,
    ParameterSpec,
    ParameterValueType,
    StrategyIR,
)
from runner.strategy_ir.routing import resolve_codegen_route


def _demo_generator(_strategy_ir: StrategyIR) -> str:
    return "# generated"


def test_resolve_codegen_route_uses_ir_generator_for_supported_engine():
    route = resolve_codegen_route(
        engine="vectorbt",
        strategy_ir=StrategyIR(name="Routing Demo"),
        generators={"vectorbt": _demo_generator},
    )

    assert route.use_ir_generator is True
    assert route.first_llm_attempt == 2
    assert route.generator_id == "vectorbt"
    assert route.generator is _demo_generator
    assert route.rollout_mode == "ir-first"
    assert route.allow_legacy_fallback is True


def test_resolve_codegen_route_uses_legacy_when_engine_not_registered():
    route = resolve_codegen_route(
        engine="pynecore",
        strategy_ir=StrategyIR(name="Routing Demo"),
        generators={"vectorbt": _demo_generator},
    )

    assert route.use_ir_generator is False
    assert route.first_llm_attempt == 1
    assert route.generator is None
    assert route.allow_legacy_fallback is True
    assert "pynecore" in route.reason


def test_resolve_codegen_route_uses_legacy_when_strategy_ir_missing():
    route = resolve_codegen_route(
        engine="vectorbt",
        strategy_ir=None,
        generators={"vectorbt": _demo_generator},
    )

    assert route.use_ir_generator is False
    assert route.first_llm_attempt == 1
    assert route.generator is None
    assert route.allow_legacy_fallback is True
    assert "StrategyIR" in route.reason


def test_resolve_codegen_route_forces_legacy_mode_even_with_generator():
    route = resolve_codegen_route(
        engine="vectorbt",
        strategy_ir=StrategyIR(name="Routing Demo"),
        generators={"vectorbt": _demo_generator},
        rollout_mode="legacy",
    )

    assert route.use_ir_generator is False
    assert route.first_llm_attempt == 1
    assert route.generator is None
    assert route.rollout_mode == "legacy"
    assert route.allow_legacy_fallback is True
    assert "forces legacy" in route.reason


def test_resolve_codegen_route_disables_legacy_fallback_in_ir_only_mode():
    route = resolve_codegen_route(
        engine="vectorbt",
        strategy_ir=StrategyIR(name="Routing Demo"),
        generators={"vectorbt": _demo_generator},
        rollout_mode="ir-only",
    )

    assert route.use_ir_generator is True
    assert route.first_llm_attempt == 2
    assert route.generator is _demo_generator
    assert route.rollout_mode == "ir-only"
    assert route.allow_legacy_fallback is False


def test_unknown_rollout_mode_fails_closed() -> None:
    """C6: unknown modes never silently fall back to ir-first."""
    import pytest

    from runner.strategy_ir.routing import _normalize_rollout_mode

    with pytest.raises(ValueError, match="Unknown rollout mode"):
        _normalize_rollout_mode("turbo")


def test_ir_only_without_ir_or_generator_raises() -> None:
    """C6: ir-only degrades loudly instead of sliding into legacy."""
    import pytest

    from runner.strategy_ir.routing import resolve_codegen_route

    with pytest.raises(ValueError, match="requires a StrategyIR"):
        resolve_codegen_route(
            engine="vectorbt", strategy_ir=None, generators={}, rollout_mode="ir-only"
        )


class TestStrategyIRSerialisation:
    """`StrategyIR`'s JSON contract had no test at all.

    Four mutations survive the suite, and the class is the hand-off between the strategy
    IR builder and the code generators - so these are the bytes two engines agree on.

    **`to_dict` dumps in python mode.** `model_dump(mode="json")` coerces enums to
    their values and dates to strings, so the result is genuinely JSON-compatible.
    Without the mode, an `EngineHint.VECTORBT` stays an enum instance and the payload
    is not what `json.dumps` was promised. It only shows up on a field that actually
    holds one, which is why the minimal fixtures never saw it.

    **`from_dict` stops validating.** `model_validate` runs every field validator and
    the model-level one; `cls(**payload)` skips them, so a hand-written or edited IR file
    loads with an empty name and duplicate parameter names.

    **`to_json` stops sorting keys.** `sort_keys=True` is what makes two IRs that differ
    only in key order serialise identically, which is what a content hash over the
    payload depends on.

    **Unique parameter names.** The model-level validator is what stops two parameters
    sharing a name, which would make the generated grid ambiguous.
    """

    def _ir(self, **overrides):
        payload = {
            "name": "Serialisation Demo",
            "engine_hint": EngineHint.VECTORBT,
            "parameters": [
                ParameterSpec(name="rsi_period", value_type=ParameterValueType.INT),
                ParameterSpec(name="stop_atr", value_type=ParameterValueType.FLOAT),
            ],
        }
        payload.update(overrides)
        return StrategyIR(**payload)

    def test_to_dict_is_json_serialisable_and_stable_under_reload(self):
        """`mode="json"` is what makes the dict genuinely JSON-shaped.

        Asserted the way the difference is actually observable rather than by
        comparing `repr`s: the dumped value must **survive a JSON round trip and reload
        as the same type**. `EngineHint` is a `str` subclass, so `json.dumps` accepts an
        enum instance and the payload still *looks* right — which is why the enum type
        alone cannot be used to detect the mutation, and why the first version of this
        test failed to kill it.

        `Decimal` is the value that separates them: without `mode="json"` a `Decimal`
        stays a `Decimal`, and `json.dumps` refuses it outright.
        """
        from decimal import Decimal

        text = self._ir().to_json()
        assert '"engine_hint": "vectorbt"' in text, text[:200]

        reloaded = json.loads(text)
        assert reloaded["engine_hint"] == "vectorbt"
        assert isinstance(reloaded["engine_hint"], str)

        # `metadata` is `dict[str, Any]`, so Pydantic applies no field-level coercion -
        # which is exactly what makes it a discriminator. A `Decimal` there serialises to
        # `0.5` under `mode="json"` and stays a `Decimal` instance without it.
        #
        # A typed field like `ParameterSpec.min_value` cannot show this: Pydantic
        # coerces it to `float` at *validation* time, before either dump mode runs. That
        # is why the first two attempts at this test could not kill the mutation.
        decimal_ir = self._ir(metadata={"threshold": Decimal("0.5")})
        payload = decimal_ir.to_dict()

        # Pydantic renders a Decimal in JSON mode as its *string* form, which is the
        # lossless representation; without the mode it stays a Decimal instance. So the
        # assertion is "no Decimal instance reaches the payload", not "it became a
        # float" - I asserted the latter first and the test caught me.
        assert not isinstance(payload["metadata"]["threshold"], Decimal), (
            "a Decimal instance survived into the payload, so it is not "
            "JSON-compatible: " + repr(payload["metadata"]["threshold"])
        )
        decimal_text = decimal_ir.to_json()
        assert "Decimal" not in decimal_text, (
            "a Decimal leaked into the JSON payload: " + decimal_text[:200]
        )
        assert json.loads(decimal_text)["metadata"]["threshold"] == "0.5"

    def test_from_json_round_trips_through_validators(self):
        original = self._ir()
        restored = StrategyIR.from_json(original.to_json())

        assert restored.name == original.name
        assert restored.engine_hint is EngineHint.VECTORBT
        assert [p.name for p in restored.parameters] == ["rsi_period", "stop_atr"]
        assert restored == original, "the IR did not survive its own JSON round trip"

    def test_to_json_is_deterministic_and_key_order_independent(self):
        """`sort_keys=True` is what makes the payload safe to hash by content.

        Two separate properties, because one alone does not kill the mutation:

        - **Determinism**: the same IR serialises byte-identically every time. `metadata`
          is a caller-supplied `dict`, and without sorting its iteration order decides the
          output. That is the property a content hash depends on, and it is the one that
          breaks first in practice.
        - **Key-order independence**: sorting means the output does not depend on the
          field order the model happened to emit.

        Note on the second: `model_dump` emits fields in *declaration* order, so two
        instances built with different keyword order already agree - which is why my
        first version of this test could not kill the mutation. `metadata` is the part
        whose order genuinely varies.
        """
        first = StrategyIR(
            name="Stable",
            engine_hint=EngineHint.PYNECORE,
            metadata={"z": 1, "a": 2, "m": 3},
        )
        second = StrategyIR(
            name="Stable",
            engine_hint=EngineHint.PYNECORE,
            metadata={"a": 2, "m": 3, "z": 1},
        )

        assert first.to_json() == second.to_json(), (
            "the same IR serialises differently depending on dict ordering, so a content "
            "hash over it is not stable:\n"
            + first.to_json()[:240]
            + "\n---\n"
            + second.to_json()[:240]
        )
        assert first.to_json() == first.to_json(), "to_json is not deterministic"

        # And repeated calls stay identical, which is what a cache or hash relies on.
        assert len({first.to_json() for _ in range(5)}) == 1

    def test_duplicate_parameter_names_are_rejected_on_construction(self):
        with pytest.raises(ValueError, match="must be unique"):
            self._ir(
                parameters=[
                    ParameterSpec(name="rsi_period"),
                    ParameterSpec(name="rsi_period"),
                ]
            )

    def test_an_empty_name_is_rejected_and_one_is_trimmed(self):
        with pytest.raises(ValueError, match="must not be empty"):
            StrategyIR(name="   ")

        # A padded name is normalised rather than rejected - both directions, so the
        # validator cannot be satisfied by rejecting everything.
        assert StrategyIR(name="  Padded  ").name == "Padded"

    def test_from_dict_rejects_a_payload_the_constructors_reject(self):
        """A hand-edited payload must not load when the same values fail in the constructor.

        Kept because `from_dict` is the seam where an edited or generated IR file enters
        the system, and it is a one-line implementation with no other contract test.

        Note: swapping `model_validate(payload)` for `cls(**payload)` turns out to be an
        **equivalent mutant** here — in Pydantic v2 `__init__` runs the same validators,
        so all three rejections below still raise either way. Verified by probing both
        paths rather than assumed, and the test is kept for the behaviour rather than
        for the mutation.
        """
        for payload, message in (
            ({"name": "   "}, "must not be empty"),
            ({"name": "Dup", "parameters": [{"name": "x"}, {"name": "x"}]}, "must be unique"),
            ({"name": "ok", "extra_key": 1}, "extra"),
        ):
            with pytest.raises(ValueError, match=message):
                StrategyIR.from_dict(payload)

        # And a valid payload still loads, so the guard is not "reject everything".
        assert StrategyIR.from_dict({"name": "Fine"}).name == "Fine"
