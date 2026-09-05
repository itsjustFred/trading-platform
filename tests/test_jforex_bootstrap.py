"""Bootstrap security/robustness tests for the JForex (Dukascopy) downloader.

Covers cleanup findings DV-B5 (first failing JVM candidate aborts startup),
DV-B6 (remote JARs loaded without integrity checking / untrusted hosts) and
DV-B7 (bootstrap failures swallowed into an empty classpath).
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path
from urllib.parse import urlparse

import pytest


# ─── DV-B6a: Dukascopy host allowlist ───────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        "https://platform.dukascopy.com/demo/jforex.jnlp",
        "https://platform.dukascopy.com/demo/jforex.jnlp/",
        "https://datafeed.dukascopy.com/datafeed/EURUSD/2024/00/01/00h_ticks.bi5",
        "https://freeserv.dukascopy.com/?x=1",
    ],
)
def test_is_allowed_dukascopy_url_accepts_dukascopy_hosts(url: str):
    from data_tools.downloaders import jforex_dl

    assert jforex_dl._is_allowed_dukascopy_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://platform.dukascopy.com/insecure.jnlp",
    ],
)
def test_is_allowed_dukascopy_url_rejects_plaintext_http(url: str):
    from data_tools.downloaders import jforex_dl

    assert jforex_dl._is_allowed_dukascopy_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example.com/jforex.jnlp",
        "https://dukascopy.com.evil.example/payload.jnlp",
        "https://example.com/?next=https://platform.dukascopy.com/demo/jforex.jnlp",
        "",
    ],
)
def test_is_allowed_dukascopy_url_rejects_non_dukascopy_hosts(url: str):
    from data_tools.downloaders import jforex_dl

    assert jforex_dl._is_allowed_dukascopy_url(url) is False


def test_jnlp_fetch_rejects_non_dukascopy_host(monkeypatch):
    """Remote resources from non-Dukascopy hosts must never be fetched."""
    from data_tools.downloaders import jforex_dl

    fetched: list[str] = []

    def fake_urlopen(url, timeout=0):  # pragma: no cover - must not be called
        fetched.append(url)
        raise AssertionError("urlopen must not be called for untrusted hosts")

    monkeypatch.setattr(jforex_dl, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="non-Dukascopy"):
        jforex_dl._parse_jnlp_jar_urls("https://evil.example.com/jforex.jnlp")

    assert fetched == []


def test_jar_download_rejects_non_dukascopy_host(monkeypatch, tmp_path: Path):
    from data_tools.downloaders import jforex_dl

    def fake_urlopen(url, timeout=0):  # pragma: no cover - must not be called
        raise AssertionError("urlopen must not be called for untrusted hosts")

    monkeypatch.setattr(jforex_dl, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="non-Dukascopy"):
        jforex_dl._download_jforex_jars_from_jnlp(
            "https://platform.dukascopy.com/demo/jforex.jnlp",
            tmp_path,
            jar_urls=["https://evil.example.com/sdk.jar"],
        )


# ─── DV-B6b: integrity verification of downloaded JARs ──────────────────────


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_verify_jar_digest_accepts_matching_digest(tmp_path: Path):
    from data_tools.downloaders import jforex_dl

    jar = tmp_path / "api.jar"
    jar.write_bytes(b"jar-bytes")
    digests = {"https://platform.dukascopy.com/api.jar": _sha256(b"jar-bytes")}

    assert jforex_dl._verify_jar_digest(jar, "https://platform.dukascopy.com/api.jar", digests)
    assert jar.exists(), "verified artifacts must not be quarantined"


def test_verify_jar_digest_quarantines_mismatch(tmp_path: Path):
    from data_tools.downloaders import jforex_dl

    jar = tmp_path / "api.jar"
    jar.write_bytes(b"tampered")
    digests = {"https://platform.dukascopy.com/api.jar": _sha256(b"expected")}

    with pytest.raises(RuntimeError, match="Integrity check failed"):
        jforex_dl._verify_jar_digest(jar, "https://platform.dukascopy.com/api.jar", digests)

    assert not jar.exists(), "mismatched artifact must be removed, not loaded"


def test_verify_jar_digest_skips_when_no_digests_configured(tmp_path: Path):
    from data_tools.downloaders import jforex_dl

    jar = tmp_path / "api.jar"
    jar.write_bytes(b"jar-bytes")

    assert jforex_dl._verify_jar_digest(jar, "https://platform.dukascopy.com/api.jar", {}) is True


def test_bootstrap_verifies_cached_jar_from_earlier_download(monkeypatch, tmp_path: Path):
    """DV-B6b: the digest check must run on the file that hits the classpath.

    A JAR that already exists on disk (e.g. cached by an earlier bootstrap run,
    or placed there by other means) must be verified as well — not only freshly
    downloaded bytes.
    """
    from data_tools.downloaders import jforex_dl

    jar_urls = ["https://platform.dukascopy.com/api.jar"]
    monkeypatch.setattr(
        jforex_dl,
        "_parse_jnlp_jar_urls",
        lambda jnlp_url: jar_urls,
    )

    def fake_urlopen(url, timeout=0):  # pragma: no cover - must not be called
        raise AssertionError("cached JAR must be verified without re-downloading")

    monkeypatch.setattr(jforex_dl, "urlopen", fake_urlopen)
    monkeypatch.setenv(
        "JFOREX_EXPECTED_JAR_SHA256",
        f"{jar_urls[0]}={_sha256(b'expected')}",
    )

    cached = tmp_path / "api.jar"
    cached.write_bytes(b"tampered-cache")

    with pytest.raises(RuntimeError, match="Integrity check failed"):
        jforex_dl._download_jforex_jars_from_jnlp(
            "https://platform.dukascopy.com/demo/jforex.jnlp", tmp_path
        )

    assert not cached.exists(), "tampered cached JAR must be quarantined"


def test_bootstrap_uses_expected_digests(monkeypatch, tmp_path: Path):
    """The JNLP bootstrap must pass the expected digest map into verification."""
    from data_tools.downloaders import jforex_dl

    jar_urls = ["https://platform.dukascopy.com/api.jar"]
    payload = b"jar-bytes"
    monkeypatch.setattr(
        jforex_dl,
        "_parse_jnlp_jar_urls",
        lambda jnlp_url: jar_urls,
    )

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    def fake_urlopen(url, timeout=0):
        assert url == jar_urls[0]
        return FakeResponse(payload)

    monkeypatch.setattr(jforex_dl, "urlopen", fake_urlopen)
    monkeypatch.setenv(
        "JFOREX_EXPECTED_JAR_SHA256",
        f"{jar_urls[0]}={_sha256(payload)}",
    )

    verified: list[tuple[str, dict]] = []

    def fake_verify(path: Path, url: str, digests: dict) -> bool:
        verified.append((url, dict(digests)))
        return True

    monkeypatch.setattr(jforex_dl, "_verify_jar_digest", fake_verify)

    downloaded = jforex_dl._download_jforex_jars_from_jnlp(
        "https://platform.dukascopy.com/demo/jforex.jnlp", tmp_path
    )

    assert [path.name for path in downloaded] == ["api.jar"]
    assert verified
    assert verified[0][0] == jar_urls[0]
    assert verified[0][1].get(jar_urls[0]) == _sha256(payload)


def test_bootstrap_aborts_on_tampered_jar(monkeypatch, tmp_path: Path):
    """A JAR that fails verification must abort the bootstrap (DV-B6/DV-B7)."""
    from data_tools.downloaders import jforex_dl

    jar_urls = ["https://platform.dukascopy.com/api.jar"]
    monkeypatch.setattr(
        jforex_dl,
        "_parse_jnlp_jar_urls",
        lambda jnlp_url: jar_urls,
    )

    class FakeResponse(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(
        jforex_dl, "urlopen", lambda url, timeout=0: FakeResponse(b"tampered")
    )
    monkeypatch.setenv(
        "JFOREX_EXPECTED_JAR_SHA256",
        f"https://platform.dukascopy.com/api.jar={_sha256(b'expected')}",
    )

    with pytest.raises(RuntimeError, match="Integrity check failed"):
        jforex_dl._download_jforex_jars_from_jnlp(
            "https://platform.dukascopy.com/demo/jforex.jnlp", tmp_path
        )


def test_expected_digest_env_overrides(monkeypatch):
    from data_tools.downloaders import jforex_dl

    monkeypatch.setenv(
        "JFOREX_EXPECTED_JAR_SHA256",
        "https://platform.dukascopy.com/a.jar=" + "a" * 64 + ","
        "https://platform.dukascopy.com/b.jar=" + "b" * 64,
    )
    digests = jforex_dl._load_expected_jar_digests()
    assert digests["https://platform.dukascopy.com/a.jar"] == "a" * 64
    assert digests["https://platform.dukascopy.com/b.jar"] == "b" * 64


# ─── DV-B5: first failing candidate must not abort startup ─────────────────


class _FakeJpypeAbortFirst:
    """JPype stand-in: the first candidate startJVM raises a Java exception."""

    def __init__(self):
        self.started = False
        self.attempts: list[str] = []

    def isJVMStarted(self) -> bool:
        return self.started

    def startJVM(self, *args, **kwargs):
        if args and isinstance(args[0], str):
            self.attempts.append(args[0])
            raise RuntimeError(f"cannot open JVM shared library {args[0]}")
        # Default (no explicit path) start succeeds.
        self.started = True


def test_ensure_jvm_started_tries_all_candidates(monkeypatch):
    from data_tools.downloaders import jforex_dl

    fake = _FakeJpypeAbortFirst()
    monkeypatch.setattr(jforex_dl, "jpype", fake)
    monkeypatch.setattr(jforex_dl, "JPYPE_AVAILABLE", True)
    monkeypatch.setattr(
        jforex_dl,
        "_resolve_jforex_classpath_details",
        lambda: (["/tmp/sdk.jar"], "test"),
    )
    monkeypatch.setattr(
        jforex_dl, "_discover_jvm_paths", lambda: ["/jvm/a", "/jvm/b"]
    )
    monkeypatch.setattr(jforex_dl, "_ensure_jvm_thread_context_class_loader", lambda: None)

    jforex_dl._ensure_jvm_started()

    assert fake.attempts == ["/jvm/a", "/jvm/b"], fake.attempts
    assert fake.started is True


def test_ensure_jvm_started_raises_with_diagnostics_when_all_fail(monkeypatch):
    from data_tools.downloaders import jforex_dl

    class _AlwaysFails(_FakeJpypeAbortFirst):
        def startJVM(self, *args, **kwargs):
            self.attempts.append(args[0] if args else "<default>")
            raise RuntimeError("boom")

    fake = _AlwaysFails()
    monkeypatch.setattr(jforex_dl, "jpype", fake)
    monkeypatch.setattr(jforex_dl, "JPYPE_AVAILABLE", True)
    monkeypatch.setattr(
        jforex_dl,
        "_resolve_jforex_classpath_details",
        lambda: (["/tmp/sdk.jar"], "test"),
    )
    monkeypatch.setattr(jforex_dl, "_discover_jvm_paths", lambda: ["/jvm/a"])

    with pytest.raises(RuntimeError, match="JVM candidates tried"):
        jforex_dl._ensure_jvm_started()


# ─── viewer fallback compatibility: bootstrap errors must stay recognizable ─


def test_viewer_fallback_fragments_cover_all_bootstrap_error_strings():
    """Bootstrap hard-failures must remain fallback-eligible in the viewer.

    The viewer's `_jforex_fallback_reason` (viewer/core.py) matches error
    fragments to decide whether to fall back to the native Dukascopy
    downloader. Every bootstrap error surface added for DV-B6/DV-B7 must be
    reachable through `jforex_dl.UNAVAILABLE_ERROR_FRAGMENTS` so a bootstrap
    failure degrades to the fallback instead of killing the download task.
    """
    from data_tools.downloaders import jforex_dl

    fragments = " | ".join(fragment.lower() for fragment in jforex_dl.UNAVAILABLE_ERROR_FRAGMENTS)
    bootstrap_errors = [
        # DV-B6a host allowlist:
        "Refusing to fetch JNLP descriptor from non-Dukascopy host: 'https://evil.example/x.jnlp'",
        # DV-B6b digest verification:
        "Integrity check failed for downloaded JAR 'https://platform.dukascopy.com/api.jar'",
        # JNLP fetch/parse:
        "Failed to fetch JNLP 'https://platform.dukascopy.com/demo/jforex.jnlp': timed out",
        "Invalid JNLP payload from 'https://platform.dukascopy.com/demo/jforex.jnlp': no root",
        # JAR download:
        "No jar resources found in JNLP 'https://platform.dukascopy.com/demo/jforex.jnlp'.",
        "Failed to download JAR 'https://platform.dukascopy.com/api.jar': timed out",
        "JNLP 'https://platform.dukascopy.com/demo/jforex.jnlp' did not resolve any downloadable JAR files.",
        # DV-B5 JVM startup:
        "Failed to start JVM for JForex SDK.\nJVM candidates tried (2):",
        "Failed to initialize JVM thread context class loader: boom",
        # Prewarm:
        "JForex SDK import failed for prewarm: boom",
        "JForex SDK prewarm failed: Instrument class is unavailable",
    ]

    missing = [
        error for error in bootstrap_errors
        if not any(fragment in error.lower() for fragment in jforex_dl.UNAVAILABLE_ERROR_FRAGMENTS)
    ]
    assert not missing, (
        "Bootstrap errors not covered by UNAVAILABLE_ERROR_FRAGMENTS "
        f"(viewer fallback would not trigger): {missing}"
    )
    assert "integrity check failed" in fragments
    assert "non-dukascopy host" in fragments


# ─── local JAR discovery (regression: helper definitions broke _discover_jars)


def test_discover_jars_finds_local_jars(tmp_path: Path):
    """Local SDK jars must still be discovered after the DV-B6 helper insert."""
    from data_tools.downloaders import jforex_dl

    (tmp_path / "sdk.jar").write_bytes(b"jar")
    lib_dir = tmp_path / "lib"
    lib_dir.mkdir()
    (lib_dir / "api.jar").write_bytes(b"jar")
    dup = tmp_path / "copy"
    dup.mkdir()
    (dup / "api.jar").write_bytes(b"jar")  # same relative name, other root
    (tmp_path / "not_a_jar.txt").write_bytes(b"x")

    discovered = jforex_dl._discover_jars(tmp_path)

    names = [path.name for path in discovered]
    assert "sdk.jar" in names
    assert "api.jar" in names
    assert len(discovered) == len(set(discovered)), "duplicates must be deduped"


def test_discover_jars_returns_empty_for_missing_root(tmp_path: Path):
    from data_tools.downloaders import jforex_dl

    assert jforex_dl._discover_jars(tmp_path / "does-not-exist") == []


# ─── DV-B7: bootstrap failures must not be swallowed ────────────────────────


def test_classpath_bootstrap_failure_is_not_swallowed(monkeypatch, tmp_path: Path):
    from data_tools.downloaders import jforex_dl

    monkeypatch.setattr(jforex_dl, "DATA_DIR", tmp_path)
    monkeypatch.delenv("JFOREX_CLASSPATH", raising=False)
    monkeypatch.delenv("JFOREX_JARS", raising=False)
    monkeypatch.delenv("JFOREX_HOME", raising=False)
    monkeypatch.setenv("DUKASCOPY_JNLP_URL", "https://platform.dukascopy.com/demo/jforex.jnlp")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        jforex_dl,
        "_download_jforex_jars_from_jnlp",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("network down")),
    )

    with pytest.raises(RuntimeError, match="network down"):
        jforex_dl._resolve_jforex_classpath_details()
