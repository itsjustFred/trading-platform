"""Upload the Dukascopy dataset to the R2 (S3-compatible) mirror.

Reads ``R2_ENDPOINT``, ``R2_ACCESS_KEY_ID``, ``R2_SECRET_ACCESS_KEY`` and
``R2_BUCKET`` from the environment (``R2_REGION`` optional, default ``auto``).

Ordering contract: data files are uploaded first and ``manifest.json`` last,
so a reader never sees a manifest pointing at objects that are not there yet.
Every uploaded object is verified with a HEAD request against the local size.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import requests

logger = logging.getLogger(__name__)

ENV_ENDPOINT = "R2_ENDPOINT"
ENV_ACCESS_KEY = "R2_ACCESS_KEY_ID"
ENV_SECRET_KEY = "R2_SECRET_ACCESS_KEY"
ENV_BUCKET = "R2_BUCKET"
ENV_REGION = "R2_REGION"
DEFAULT_REGION = "auto"
MANIFEST_NAME = "manifest.json"


def r2_config_from_env(env: dict[str, str] | None = None) -> dict[str, str]:
    values = os.environ if env is None else env
    missing = [
        name
        for name in (ENV_ENDPOINT, ENV_ACCESS_KEY, ENV_SECRET_KEY, ENV_BUCKET)
        if not str(values.get(name) or "").strip()
    ]
    if missing:
        raise RuntimeError(f"Missing R2 configuration: {', '.join(missing)}")
    return {
        "endpoint": str(values[ENV_ENDPOINT]).strip().rstrip("/"),
        "access_key": str(values[ENV_ACCESS_KEY]).strip(),
        "secret_key": str(values[ENV_SECRET_KEY]).strip(),
        "bucket": str(values[ENV_BUCKET]).strip(),
        "region": str(values.get(ENV_REGION) or DEFAULT_REGION).strip() or DEFAULT_REGION,
    }


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str) -> bytes:
    k_date = _sign(("AWS4" + secret).encode("utf-8"), datestamp)
    k_region = _sign(k_date, region)
    k_service = _sign(k_region, "s3")
    return _sign(k_service, "aws4_request")


class R2Client:
    """Minimal SigV4-signed S3 client (PUT + HEAD) for Cloudflare R2."""

    def __init__(self, config: dict[str, str], *, session=None):
        self.endpoint = config["endpoint"]
        self.bucket = config["bucket"]
        self.region = config.get("region") or DEFAULT_REGION
        self._access_key = config["access_key"]
        self._secret_key = config["secret_key"]
        if session is None:
            session = requests.Session()
        self._session = session

    def _url(self, key: str) -> str:
        return f"{self.endpoint}/{self.bucket}/{key}"

    def _signed_headers(
        self, method: str, key: str, payload_hash: str
    ) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        canonical_uri = f"/{self.bucket}/{key}"
        canonical_headers = (
            f"host:{self.endpoint.split('://', 1)[-1]}\n"
            "x-amz-content-sha256:" + payload_hash + "\n"
            "x-amz-date:" + amz_date + "\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join(
            [method, canonical_uri, "", canonical_headers, signed_headers, payload_hash]
        )
        scope = f"{datestamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            [
                "AWS4-HMAC-SHA256",
                amz_date,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            _signing_key(self._secret_key, datestamp, self.region),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return {
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": (
                f"AWS4-HMAC-SHA256 Credential={self._access_key}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }

    def _request(
        self,
        method: str,
        key: str,
        *,
        body=None,
        payload_hash: str | None = None,
    ) -> requests.Response:
        if payload_hash is None:
            payload_hash = hashlib.sha256(body or b"").hexdigest()
        headers = self._signed_headers(method, key, payload_hash)
        return self._session.request(
            method, self._url(key), data=body, headers=headers, timeout=120
        )

    def put_file(self, local_path: Path, key: str) -> int:
        # Hash and stream through one open handle so the signed payload hash
        # always describes the bytes actually sent (same inode, no TOCTOU).
        digest = hashlib.sha256()
        size = 0
        with open(local_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
                size += len(chunk)
            fh.seek(0)
            resp = self._request(
                "PUT", key, body=fh, payload_hash=digest.hexdigest()
            )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"R2 PUT {key} failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return size

    def head_size(self, key: str) -> int | None:
        resp = self._request("HEAD", key)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError(f"R2 HEAD {key} failed: HTTP {resp.status_code}")
        length = resp.headers.get("Content-Length")
        return int(length) if length is not None else None


def dataset_upload_plan(root: str | Path) -> tuple[list[str], str]:
    """``(data keys, manifest key)`` the remote mirror should hold.

    Derived from ``manifest.json`` — every file it lists plus the manifest
    itself — so a rerun after a partial upload re-sends whatever is missing
    or size-drifted instead of trusting a publish list that only covers the
    latest run.
    """
    root = Path(root)
    manifest = json.loads((root / MANIFEST_NAME).read_text())
    data: list[str] = []
    for symbol, entry in manifest.items():
        if not isinstance(entry, dict):
            continue
        data.extend(f"{symbol}/{fname}" for fname in (entry.get("files") or {}))
    return sorted(data), MANIFEST_NAME


def upload_dataset(
    root: str | Path,
    *,
    client: R2Client | None = None,
    env: dict[str, str] | None = None,
    log: Callable[[str], None] = logger.info,
) -> list[str]:
    """Mirror the dataset to R2, decided from local vs remote state.

    Every file listed in ``manifest.json`` is HEAD-checked against the remote
    object size; missing or size-drifted objects are PUT and re-verified.
    ``manifest.json`` is always uploaded last, after every data file it lists
    is confirmed present — a reader never sees a manifest pointing at objects
    that are not there. Safe to re-run: matching objects are skipped.
    Returns the list of uploaded keys. Raises on the first failure.
    """
    root = Path(root)
    if client is None:
        client = R2Client(r2_config_from_env(env))

    data_files, manifest_key = dataset_upload_plan(root)
    uploaded: list[str] = []
    for rel in data_files:
        local = root / rel
        size = local.stat().st_size
        if client.head_size(rel) == size:
            log(f"unchanged {rel} ({size} bytes)")
            continue
        client.put_file(local, rel)
        remote_size = client.head_size(rel)
        if remote_size != size:
            raise RuntimeError(
                f"R2 size mismatch for {rel}: local {size} != remote {remote_size}"
            )
        uploaded.append(rel)
        log(f"uploaded {rel} ({size} bytes)")

    manifest_local = root / manifest_key
    client.put_file(manifest_local, manifest_key)
    remote_size = client.head_size(manifest_key)
    if remote_size != manifest_local.stat().st_size:
        raise RuntimeError(
            f"R2 size mismatch for {manifest_key}: "
            f"local {manifest_local.stat().st_size} != remote {remote_size}"
        )
    uploaded.append(manifest_key)
    log(f"uploaded {manifest_key}")
    return uploaded
