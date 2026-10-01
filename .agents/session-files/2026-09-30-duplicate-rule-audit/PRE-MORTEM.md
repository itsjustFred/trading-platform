# Pre-Mortem Report — duplicate-rule audit session

**Scope:** production files changed by this session, per `platform-pr-verify` step 3.
- `risk/models.py` (208 lines)
- `data_tools/downloaders/alpaca_dl.py` (362 lines)
- `data_tools/downloaders/huggingface_dl.py` (405 lines)

**Date:** 2026-09-30

## Summary

Six post-mortems. The dominant theme is **stringly-typed contracts between a writer and a reader
that nothing keeps in agreement**. `realized_risk_amount` is written in one place, read in three,
and the reads do not have the same shape — which is defensible today only because each reader
happens to see a different source. The second theme is **load-bearing literals in code I added
this session**, written down here rather than only fixed, because a pin that can be defeated by a
reasonable future edit is not a pin.

Two of the six are fragilities in my own new work. That is the point of running this pipeline on
one's own commits rather than only on someone else's.

**Nothing in this report is a live bug.** One candidate looked like a live risk figure bug and
was not — see post-mortem 1, where the measurement contradicted the hypothesis and the
hypothesis was the wrong one.

---

## Post-Mortems

### 1. The realized-risk reader drifts narrower than its writer, and falls back to the policy figure

**Severity:** High
**Component:** `risk/models.py::realized_risk_amount`, `risk/engine.py:385`, `viewer/core.py::_trade_realized_risk`, `shared/database.py:~5020`
**Fragility type:** Stringly-typed contract (3) + coincidental correctness (5)

#### What happened

A capped fill is reported as having risked the full policy amount on every trade. Risk reports
show `risk_amount=500.0` and `risk_percent=0.005` for a position where the notional cap limited
the real exposure to 80.0. Every downstream consumer — stats, the validation-parity report, the
live metadata payload — inherits the overstatement, and because nothing is obviously *wrong*
(numbers are present, finite, positive) nobody investigates. Portfolio risk looks conservative,
which is the direction nobody double-checks.

#### The change that caused it

Someone tidying the metadata payload in `risk/engine.py`, moving the realized-risk fields under
the existing `risk_math` sub-dict so that risk data has a single home rather than sitting beside
it. The motivation is entirely reasonable: `risk_math` already holds `equity_at_entry`,
`capped_by_notional` and the stop provenance, and two flat keys next to a nested block of related
fields is untidy.

The same commit updates `viewer/core.py::_trade_realized_risk` and the `shared/database.py`
reader, because both already look in `risk_math` first and so *keep working without any change*.
That is precisely what makes it dangerous: nothing fails, because the two readers that consume
persisted trade metadata were never broken.

#### Why it broke

`risk/models.py:183` reads only one location:

```python
realized = metadata.get("realized_risk_amount")
```

It does not look in `risk_math`. `viewer/core.py:469-473` and `shared/database.py:5027-5030` both
read nested-first with a flat fallback. Three copies of one rule, with two different lookup
shapes.

This is *correct today*, which is the trap. `risk/models.py` only ever receives
`RiskDecision.metadata`, and the only live writer of that (`risk/engine.py:376-389`) writes the
key flat. The nested `risk_math` shape belongs to persisted backtest trade metadata, produced by
a different path entirely. So each reader matches its own source and the discrepancy is invisible.

The invariant that actually matters — *the writer's key placement matches the reader's* — is
enforced by nothing. `tests/test_risk_engine.py:217` asserts the writer's flat placement, so a
move would fail there; but nothing asserts that `realized_risk_amount` *reads* flat, so the reader
could be narrowed further, or the writer's key renamed, in a way that passes.

Measured on the current code with a top-level key, which is what the live path actually produces:

```
top-level metadata  -> amount    80.00  percent 0.000800   correct
nested under risk_math -> amount 500.00  percent 0.005000   (falls back to the policy figure)
```

The function's own docstring states the consequence: *"Consumers must read this, never the policy
figure, or they overstate risk on every capped fill."* The fallback at `risk/models.py:192`
returns `float(order.risk_amount)` — the policy figure — so a key-shape change silently produces
exactly the outcome the function exists to prevent.

#### How it was caught

Not caught. Every test would pass. `realized_risk_amount` is not referenced by any test in the
repository (checked: `grep` over `tests/` finds the key only in writer-side and viewer-side
assertions, never an import of this reader). The failure is silent, numeric, and in the
conservative direction, which is the hardest class to notice.

#### Hardening suggestions

- Add a test that constructs a `SizedOrder` whose `decision.metadata` carries the flat key, as
  `risk/engine.py` writes it, and asserts `realized_risk_amount` returns it. One test, and it
  pins writer/reader agreement rather than either in isolation.
- Better: assert the agreement directly — call the engine, take the metadata it actually
  produced, and feed that same object to the reader. That makes the invariant structural instead
  of restating the current shape twice.
- Either give `realized_risk_amount` the same nested-first lookup as its two siblings, or add a
  comment stating that it deliberately reads only the live `RiskDecision` shape and why the
  nested shape cannot reach it. The asymmetry is defensible; being unaware of it is not.

---

### 2. `TradeSignal.identity` is excluded from equality but load-bearing in the dedup key

**Severity:** Medium
**Component:** `risk/models.py:83` (`identity: str = field(..., compare=False)`), `risk/engine.py:79-85`
**Fragility type:** Invisible invariants (7)

#### What happened

Two distinct signals are treated as one. A signal replayed from the same Discord message twice
produces two `TradeSignal` objects with different `identity` values; `==` says they are equal, so
a de-duplication path that uses `==` collapses them and the second execution is silently dropped.
Trade logs show one signal where the dispatcher sent two.

#### The change that caused it

Someone adding a natural-key dedup helper, writing `if existing == candidate: continue` because
it is the obvious expression and `TradeSignal` is a dataclass. The change is small, reads
correctly, and passes review.

#### Why it broke

```python
identity: str = field(default_factory=lambda: uuid4().hex, repr=False, compare=False)
```

`compare=False` excludes identity from `__eq__`. Confirmed empirically:

```
a.identity=aaaaaaaa  b.identity=bbbbbbbb
a == b -> True        # different identities, equal dataclass
```

But `risk/engine.py:82` builds the id that actually distinguishes signals:

```python
signal_id = ("legacy:" f"{signal.symbol}:{signal.timeframe}:{signal.action}:"
             f"{signal.entry_price:.12g}:{stop}:{take_profit}:{signal.identity}")
```

So the engine treats identity as decisive while the dataclass says identity does not exist. Both
are deliberate — identity is a uuid4, so including it would make every signal unequal and defeat
value-equality — but nothing records that the equality relation and the id relation disagree.
`TradeSignal` is unhashable (`metadata: dict` forces `__hash__ = None`), which narrows the
accidental surface, but `==` remains directly usable.

#### How it was caught

Not caught. No test asserts that two signals with different identities are equal or unequal, so
either behaviour can change silently.

#### Hardening suggestions

- Add a test pinning the current semantics explicitly — `test_signals_with_different_identities_compare_equal` —
  so the next reader learns it is intentional. The comment says `compare=False`; a test says it.
- Add a one-line note at `risk/engine.py:79` that the id deliberately includes a field equality
  excludes, because that inversion is the trap.
- If value-dedup is genuinely wanted, provide it explicitly (compare the tuple of the fields the
  engine's id uses) rather than relying on `__eq__`, whose semantics are about a different
  question.

---

### 3. The pin table's expected-copy counts are load-bearing integers I introduced

**Severity:** Low
**Component:** `tests/test_duplicate_rule_implementations.py::DUPLICATED_RULES`
**Fragility type:** Load-bearing defaults (8) + coincidental correctness (5)

#### What happened

A fourth downloader is added — a new provider with its own `_to_utc_timestamp`, written by
copying the existing one, which is exactly what the pin exists to make safe. CI fails on
`test_every_copy_of_a_duplicated_rule_agrees` with `expected 3 identical implementation(s),
found 4`. The developer concludes the pin is wrong, deletes the entry, and the duplication the
pin was protecting against is now unbounded.

#### The change that caused it

Adding a Dukascopy-or-equivalent downloader module, and copying `_to_utc_timestamp` from
`alpaca_dl.py` rather than extracting it. Entirely reasonable — the module already exists in
`data_tools/downloaders/` and the pattern is established.

#### Why it broke

`DUPLICATED_RULES` pairs each rule name with an integer count. The count is not derived; it is
asserted. So the pin treats "a new copy exists" as a failure even when the new copy is identical
and harmless, and the failure message says `expected 3` without saying that raising the count is
a legitimate response.

This is a defect in the pin's design, found by running a pre-mortem over my own commit rather
than only over the code I was auditing. The count was chosen because a count is the only way to
catch a copy being *deleted*; the cost is that it also punishes a copy being *added*.

#### How it was caught

It would be caught — that is the problem. The pin fires correctly per its own contract and
misleadingly in practice.

#### Hardening suggestions

- Reword the count assertion so the message names both valid responses: adding a justified copy
  (raise the count here) versus a copy that drifted (fix the code). The current text only names
  the second.
- Better: assert `>= expected_copies` copies exist *and* that all bodies agree, plus one separate
  test per rule asserting the exact count. Then adding a legitimate copy fails only the specific
  count test, which can be updated deliberately, while a *divergent* copy still fails loudly.
- Alternatively, treat the count as a floor and add a separate "no unexplained copies" check that
  requires a registry entry, so the decision to accept a copy is recorded rather than made by
  deletion.

---

### 4. The alias-coverage invariant spans two modules and is enforced only by a test in a third

**Severity:** Low
**Component:** `shared/constants.py::TIMEFRAME_ALIASES`, `data_tools/downloaders/*_dl.py::TIMEFRAME_MAP`
**Fragility type:** Invisible invariants (7)

#### What happened

An alias `4hour -> 4h` is added to `shared/constants.TIMEFRAME_ALIASES`, which is where three
canonical aliases live. Alpaca and HuggingFace downloads start accepting `4hour`; yfinance
rejects it. The same configured timeframe succeeds against one provider and fails against
another, and the operator sees a provider-specific error for a value the platform documents as
valid.

#### The change that caused it

Someone extending timeframe normalization because a provider began emitting `4hour`. They add it
in the one place that looks canonical, `shared/constants.py`, and move on.

#### Why it broke

`normalize_timeframe` translates only what is in `TIMEFRAME_ALIASES`, but every downloader only
accepts keys in its **own** `TIMEFRAME_MAP`. The invariant that makes those two agree is that
every alias is *also* a direct map key, so the alias never has to be translated to be accepted.
That is a relationship between a constant in `shared/constants.py` and a dict in each of three
downloader modules, and nothing in production code asserts it.

This was discovered while removing a dead second lookup from two of the three copies — the lookup
was unreachable *because* the invariant holds, which is exactly the kind of correctness that looks
like an accident. The invariant is now pinned, but only in
`tests/test_duplicate_rule_implementations.py`, which is a third location unrelated by name to
either side.

#### How it was caught

Caught, by `test_every_timeframe_alias_is_a_direct_key_in_each_downloader_map`. That test is the
only thing standing between an alias addition and a three-way behavioural split, and it lives far
from the constant it constrains.

#### Hardening suggestions

- Derive rather than assert: have each downloader's `TIMEFRAME_MAP` be built from the canonical
  alias table plus its provider-specific extras, so an alias cannot be added in one place and
  missed in another. This is the fix the grid downloader already uses via
  `GRID_TIMEFRAME_ALIASES`.
- If derivation is not wanted, add the alias keys to `TIMEFRAME_MAP` in the same module by
  construction, and put the consistency check next to the constant rather than in a test file.

---

### 5. A derived percent silently becomes the policy percent when the balance is unusable

**Severity:** Medium
**Component:** `risk/models.py::realized_risk_percent` (195-208)
**Fragility type:** Load-bearing defaults (8) + coincidental correctness (5)

#### What happened

Realized risk percentage is reported as the policy percentage on every trade for one account. The
two quantities are not the same and are not close — on a capped fill the policy figure overstates
by the cap ratio. Reports show a consistent, plausible-looking risk percent, and because the
substitution is silent there is no indication that the derivation was skipped.

#### The change that caused it

Someone adds an early guard for a new caller that does not have an account balance:

```python
try:
    balance = float(account_balance)
except (TypeError, ValueError):
    balance = 0.0
```

so a `None` balance cannot raise. The intent is to make the function total. Reasonable.

#### Why it broke

```python
if balance > 0:
    return float(realized_risk_amount(order)) / balance
return float(order.risk_percent)
```

A zero, negative, `None` or non-numeric balance returns `order.risk_percent` — the **policy**
figure — from a function named `realized_risk_percent`. The two are different quantities by
construction: the docstring above says the engine reports no realized percent, so this function
*derives* it. On the fallback path it stops deriving and reports the policy number under the
derived name, and the caller cannot tell.

The name is the hazard. `realized_risk_percent` returning a policy figure is indistinguishable at
the call site from the real thing.

#### How it was caught

Not caught. **No test calls `realized_risk_percent`.** The string appears once in
`tests/test_viewer_stats_contract.py:332`, but as a metadata *key* in a fixture dict for the
viewer path, never as a call to this function — so the function has zero call-site coverage.
The failure is silent and in the safe-looking direction.

#### Hardening suggestions

- Return `None` for "cannot derive" and make callers decide, rather than substituting a different
  quantity. If a fallback is genuinely wanted, name it: `_percent_or_policy(...)`, so the
  substitution is visible at the definition and at every call site.
- Add tests for the unusable-balance paths, which currently have none, asserting whichever
  behaviour is chosen — and assert it *differently* from the derived case so the two cannot
  collapse into each other.

---

### 6. `_parse_bool` refuses numpy scalars, so a decision built from a DataFrame column cannot be constructed

**Severity:** Low
**Component:** `risk/models.py::_parse_bool` (60-71), `RiskDecision.accepted`
**Fragility type:** Stringly-typed contracts (3) / version-coupled assumptions (10)

#### What happened

A `RiskDecision` is built with `accepted` taken from a pandas DataFrame column and the
construction raises `ValueError: accepted must be a boolean`, several layers from the cause. The
column holds `True`; the value is `numpy.bool_`, and `numpy.bool_` is not a Python `bool`.

#### The change that caused it

Nothing — this is latent. The scenario is a future caller sourcing `accepted` from data rather
than from a literal.

#### Why it broke

```python
if isinstance(value, bool): ...
if isinstance(value, str): ...
if isinstance(value, int) and value in {0, 1}: ...
```

Measured:

```
True          (bool)     -> True
1             (int)      -> True
np.True_      (bool)     -> raises ValueError
np.int64(1)   (int64)    -> raises ValueError
np.float64(1) (float64)  -> raises ValueError
```

`isinstance(np.bool_(True), int)` is `False` and `isinstance(np.int64(1), int)` is `False` — numpy
scalars are not subclasses of the Python builtins they represent. The `bool` check runs first, so
`np.bool_` misses it, and the `int` check misses it too. The `{0, 1}` restriction is also
undocumented and would reject `np.int64(2)` even if the type check passed.

It fails **closed**, so nothing unsafe happens — the cost is a refusal with a misleading message,
in a codebase that otherwise passes numpy values through pandas boundaries without friction.

#### How it was caught

Found while writing `tests/test_risk_models.py` for the mutation pass: the new test pins Python
bools and ints, and the adjacent question — what about the numpy equivalents — turned out to have
the answer "raises".

#### Hardening suggestions

- If numpy scalars should be accepted, say so explicitly and document it: extend the `int` branch
  to `numbers.Integral` and `bool`-like values, or convert through `bool()` behind an explicit
  membership check. Keep the `{0, 1}` restriction and say why it exists.
- If they should not be accepted, raise a message naming the type received, so the failure points
  at the pandas boundary instead of at `accepted`. Today the message is the same one a caller sees
  for `"maybe"`, which sends them looking for a string-parsing bug that does not exist.
- Either way, one test covering `np.bool_`/`np.int64` fixes the ambiguity permanently.

---

## Themes and recommendations

**The writer/reader key agreement is the systemic one.** Post-mortems 1 and 5 are the same
underlying issue: a derived quantity has a function that silently substitutes a *different*
quantity when its inputs are unusable, and the substitution is invisible because both values are
`float`. `realized_risk_amount` returns `order.risk_amount`; `realized_risk_percent` returns
`order.risk_percent`. In both cases the fallback is the policy figure, and in both cases the
docstring says the policy figure must never be reported as the realized one.

Two structural moves address both:

1. **Make "cannot compute" a representable return value** (`None`) rather than a substitution.
   Callers then have to decide, and the decision is visible in their code.
2. **Derive the metadata shape in one place.** The `risk_math` vs flat-key split between the live
   path and the persisted path is the root of post-mortem 1, and it exists because two paths grew
   their own shape independently. A single constructor for risk metadata would make the reader's
   narrow lookup correct by construction instead of by coincidence.

**Load-bearing literals are this session's own recurring defect.** Post-mortems 3 and 4 are both
integers and table relationships I introduced to close gaps, and both are enforced only by a test
sitting far from what they constrain. That is the honest cost of the pin approach the notes
adopt: a static pin is the right instrument for a duplicated *spelling*, but it shifts the
invariant into a file nobody reads when editing the constant. Where derivation is possible —
`GRID_TIMEFRAME_ALIASES` is the existing precedent — it is strictly better than a pin, and post-
mortem 4 says so.

**A note on what this pass did not produce.** No current bug. The one candidate that looked like a
live overstatement of risk (post-mortem 1) was measured before being written up, and the
measurement contradicted the hypothesis: the live writer puts the key flat, which is the shape the
narrow reader expects, so the reported ~6x overstatement is only reachable if the writer's shape
changes. That is recorded as a fragility rather than an incident, which is the distinction the
whole exercise turns on.