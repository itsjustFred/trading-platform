"""Every module that caches DATA_DIR must agree on it.

Six modules hold a `DATA_DIR` at import time. Two of them *resolve* it -
`shared.constants` and `shared.database`, independently, which is why they can
disagree. The other four import or derive from `constants`, so they are only
indirectly at risk; they are checked anyway because a derived path is exactly
where a hardcoded one creeps in, and `data_tools/dolthub_manager.DOLT_DIR` is a
demonstrated example of that going wrong.

Either way, a test that rebinds the variable with `monkeypatch.setenv` after these
modules are imported has not changed what they use - and the conftest fixtures do
exactly that, because a fixture cannot un-import anything.

The repo already has a drift detector, but it compares two modules
(`shared.database` against `shared.constants`) and says nothing about the other
four. This is not hypothetical bookkeeping: a persisted task record that escaped
the test sandbox once made a full suite run clone multi-GB dolthub repositories
into the working tree, and the mechanism was a module reading real paths while the
test believed it was in a tmp dir.

So this spawns a fresh process with `DATA_DIR` set *before* anything is imported -
which is how the real platform runs - and asks every caching module where it
thinks the data lives. A module that disagrees is a module a test cannot isolate.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

#: Modules holding a DATA_DIR at module scope, so their value is fixed by whatever
#: the environment said at import. `shared.constants` and `shared.database` resolve
#: it independently; the rest import or derive from `constants`.
CACHING_MODULES = [
    "shared.constants",
    "shared.database",
    "viewer.main",
    "runner.cli",
    "data_tools.dolthub_manager",
    "data_tools.cache_manager",
]

_PROBE = textwrap.dedent(
    """
    import importlib, json, pathlib, sys

    repo_data = pathlib.Path(sys.argv[1]) / "data"
    target = sys.argv[2]
    out = {}
    for name in json.loads(sys.argv[3]):
        try:
            module = importlib.import_module(name)
        except Exception as exc:
            out[name] = {"data_dir": f"import failed: {type(exc).__name__}", "leaks": []}
            continue
        value = getattr(module, "DATA_DIR", None)
        if value is None:
            value = getattr(module, "_data_dir", None)
        # Every module-level Path, not just DATA_DIR. A module can hold an
        # isolated DATA_DIR and still hardcode the path that actually matters -
        # DOLT_DIR is the demonstrated example, and it is what filled the working
        # tree with multi-GB clones.
        leaks = []
        for attr in dir(module):
            if attr.startswith("__"):
                continue
            try:
                candidate = getattr(module, attr)
            except Exception:
                continue
            if not isinstance(candidate, pathlib.PurePath):
                continue
            try:
                inside = candidate.resolve().is_relative_to(repo_data.resolve())
            except (OSError, ValueError):
                continue
            if inside and target not in str(candidate):
                leaks.append(f"{attr}={candidate}")
        out[name] = {
            "data_dir": str(value) if value is not None else None,
            "leaks": sorted(leaks),
        }
    print(json.dumps(out))
    """
)


def _probe(data_dir: str, modules: list[str]) -> dict[str, str | None]:
    env = dict(os.environ)
    env["DATA_DIR"] = data_dir
    env.pop("TRADING_DATA_DIR", None)
    env.pop("LOG_DIR", None)
    repo_root = subprocess.run(
        [sys.executable, "-c", "import shared.constants as c; print(c.PROJECT_ROOT)"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
    )
    # Checked before its stdout is used. A failed lookup prints nothing, and the
    # probe would then be handed an empty project path - so the failure surfaces
    # as "the probe failed", pointing at the isolation check rather than at the
    # lookup that actually broke. The two also shared no cwd, which is not why
    # this went unnoticed but is one fewer difference between them.
    project_root = repo_root.stdout.strip()
    assert repo_root.returncode == 0 and project_root, (
        f"could not resolve PROJECT_ROOT, so the probe was handed "
        f"{project_root!r} and any failure below would point at the wrong thing:\n"
        f"{repo_root.stderr[-800:]}"
    )
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE,
            project_root,
            data_dir,
            json.dumps(modules),
        ],
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
        cwd=str(__import__("pathlib").Path(__file__).resolve().parent.parent),
    )
    assert done.returncode == 0, (
        f"the probe itself failed, so this test would be asserting nothing:\n"
        f"{done.stderr[-800:]}"
    )
    return json.loads(done.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("module_name", CACHING_MODULES)
def test_a_module_honours_DATA_DIR_from_the_environment(module_name, tmp_path):
    """The env var must win, in a fresh process, for every module that caches it.

    Checked per module so a failure names the one that drifted instead of listing
    a set. `viewer.main` is imported for its side-effect-free module body only -
    it builds an app object, which touches no database.
    """
    target = tmp_path / "isolated-data"
    target.mkdir()

    result = _probe(str(target), [module_name])[module_name]
    resolved = result["data_dir"]

    assert resolved is not None, (
        f"{module_name} exposes no DATA_DIR to check; if it was renamed, update "
        "CACHING_MODULES rather than deleting the entry - an unchecked module is "
        "exactly what this is looking for"
    )
    assert not str(resolved).startswith("import failed"), (
        f"{module_name} could not be imported in isolation: {resolved}"
    )
    assert str(target) in str(resolved), (
        f"{module_name} resolved DATA_DIR to {resolved!r} while the environment said "
        f"{str(target)!r}. A test that rebinds DATA_DIR cannot isolate a module that "
        "cached its path at import, so anything this module reads or writes during a "
        "test is real data, not tmp data."
    )
    assert not result["leaks"], (
        f"{module_name} holds module-level paths inside the repository's real data "
        f"directory while the environment pointed DATA_DIR at {str(target)!r}: "
        f"{result['leaks']}. A module can carry an isolated DATA_DIR and still "
        "hardcode the path that actually does the writing - DOLT_DIR is the "
        "demonstrated example, and it filled a working tree with multi-GB clones "
        "while every test believed it was in a tmp dir."
    )


def test_no_caching_module_silently_falls_back_to_the_repo_data_dir(tmp_path):
    """One process, all six at once - the situation a test session actually creates.

    The per-module test above catches a module that ignores the environment. This
    catches a module that honours it *and* another that does not, in the same
    process, which is how a mixed state arises: one module writes to tmp, another
    reads the repo's data dir, and the two disagree about what the test is doing.
    """
    target = tmp_path / "shared-data"
    target.mkdir()

    resolved = _probe(str(target), CACHING_MODULES)

    strays = {
        name: result
        for name, result in resolved.items()
        if result["data_dir"] is None
        or str(result["data_dir"]).startswith("import failed")
        or str(target) not in str(result["data_dir"])
    }
    assert not strays, (
        "these modules do not all resolve DATA_DIR to the same place, so a test "
        f"session would be working against more than one data directory: {strays}"
    )

    leaks = {name: r["leaks"] for name, r in resolved.items() if r["leaks"]}
    assert not leaks, (
        "these modules hold paths inside the repository's real data directory even "
        f"though DATA_DIR pointed elsewhere: {leaks}"
    )
