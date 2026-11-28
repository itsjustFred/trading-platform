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

    def _request(self, method: str, key: str, *, body: bytes | None = None) -> requests.Response:
        payload_hash = hashlib.sha256(body or b"").hexdigest()
        headers = self._signed_headers(method, key, payload_hash)
        return self._session.request(
            method, self._url(key), data=body, headers=headers, timeout=120
        )

    def put_file(self, local_path: Path, key: str) -> int:
        body = local_path.read_bytes()
        resp = self._request("PUT", key, body=body)
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"R2 PUT {key} failed: HTTP {resp.status_code} {resp.text[:200]}"
            )
        return len(body)

    def head_size(self, key: str) -> int | None:
        resp = self._request("HEAD", key)
        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            raise RuntimeError(f"R2 HEAD {key} failed: HTTP {resp.status_code}")
        length = resp.headers.get("Content-Length")
        return int(length) if length is not None else None


def ordered_upload_paths(published: list[str]) -> tuple[list[str], str | None]:
    """Split published rel-paths into (data files, manifest name or None).

    ``manifest.json`` is always uploaded last regardless of input order.
    """
    data = [p for p in published if p != MANIFEST_NAME]
    manifest = MANIFEST_NAME if MANIFEST_NAME in published else None
    return data, manifest


def upload_dataset(
    root: str | Path,
    published: list[str],
    *,
    client: R2Client | None = None,
    env: dict[str, str] | None = None,
    log: Callable[[str], None] = logger.info,
) -> list[str]:
    """Upload ``published`` files (paths relative to ``root``) to R2.

    Data files go first, ``manifest.json`` last; every object is HEAD-verified
    against the local size. Returns the list of uploaded keys. Raises on the
    first failure — callers must treat that as an incomplete mirror.
    """
    root = Path(root)
    if client is None:
        client = R2Client(r2_config_from_env(env))

    data_files, manifest = ordered_upload_paths(published)
    ordered = list(data_files)
    if manifest:
        ordered.append(manifest)

    uploaded: list[str] = []
    for rel in ordered:
        local = root / rel
        size = local.stat().st_size
        client.put_file(local, rel)
        remote_size = client.head_size(rel)
        if remote_size != size:
            raise RuntimeError(
                f"R2 size mismatch for {rel}: local {size} != remote {remote_size}"
            )
        uploaded.append(rel)
        log(f"uploaded {rel} ({size} bytes)")
    return uploaded
