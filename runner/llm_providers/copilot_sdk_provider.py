from __future__ import annotations

import asyncio
import logging
import os
import time
from importlib import import_module
from threading import Event, Thread
from typing import Any

logger = logging.getLogger(__name__)

AUTH_SESSION_ERROR = (
    "copilot_sdk provider is configured but no valid Copilot SDK auth/session is available"
)
DEFAULT_COPILOT_SDK_MODEL = "gpt-5-mini"

# Deliberate trust posture (F27, Decision Record 2026-08-31): the Copilot SDK
# session installs PermissionHandler.approve_all, which grants every requested
# tool permission without prompting. This provider only asks the model for
# *generated strategy code* — no repository writes, no shell access — and the
# returned text is screened (runner.codegen_safety) and gated behind a
# SHA256-bound human review (runner.approval_gate) before it can ever run. If
# this provider is ever reused for agentic coding tasks, approve_all must be
# replaced with a real permission handler.
PERMISSION_POSTURE_NOTE = (
    "PermissionHandler.approve_all is deliberate: this provider only solicits "
    "generated strategy code (never executed unscreened), so every tool "
    "permission is auto-approved for the duration of the session."
)
MODEL_ALIASES: dict[str, str] = {
    "gpt-5": "gpt-5.2",
    "gpt-5.3-codex": "gpt-5-codex",
    "gpt-5.2-codex": "gpt-5-codex",
    "gpt-5.4-mini": "gpt-5-mini",
}


def _load_copilot_sdk() -> tuple[type[Any], Any]:
    copilot_module = import_module("copilot")
    session_module = import_module("copilot.session")
    return copilot_module.CopilotClient, session_module.PermissionHandler


def _extract_content(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if isinstance(payload, dict):
        for key in ("content", "text", "message", "response", "output", "data", "result"):
            extracted = _extract_content(payload.get(key))
            if extracted:
                return extracted
        for value in payload.values():
            extracted = _extract_content(value)
            if extracted:
                return extracted
        return ""
    if isinstance(payload, (list, tuple, set)):
        for item in payload:
            extracted = _extract_content(item)
            if extracted:
                return extracted
        return ""

    for attr in ("content", "text", "message", "response", "output", "data", "result"):
        if hasattr(payload, attr):
            extracted = _extract_content(getattr(payload, attr))
            if extracted:
                return extracted
    return ""


def _extract_model_ids(payload: Any) -> list[str]:
    if payload is None:
        return []
    if isinstance(payload, dict):
        ids: list[str] = []
        if isinstance(payload.get("id"), str):
            ids.append(payload["id"].strip())
        for key in ("models", "data", "items", "results"):
            ids.extend(_extract_model_ids(payload.get(key)))
        return [value for value in ids if value]
    if isinstance(payload, (list, tuple, set)):
        items: list[str] = []
        for item in payload:
            items.extend(_extract_model_ids(item))
        return [value for value in items if value]

    values: list[str] = []
    model_id = getattr(payload, "id", None)
    if isinstance(model_id, str):
        values.append(model_id.strip())
    for attr in ("models", "data", "items", "results"):
        if hasattr(payload, attr):
            values.extend(_extract_model_ids(getattr(payload, attr)))
    return [value for value in values if value]


def _is_auth_error(error: BaseException) -> bool:
    message = str(error).strip().lower()
    markers = (
        "auth",
        "oauth",
        "credential",
        "token",
        "not signed",
        "sign in",
        "login",
        "unauthorized",
        "forbidden",
        "permission denied",
        "401",
        "403",
    )
    return any(marker in message for marker in markers)


def _is_model_error(error: BaseException) -> bool:
    message = str(error).strip().lower()
    return "model" in message and any(
        marker in message
        for marker in ("unsupported", "unknown", "not found", "invalid", "unavailable")
    )


def _apply_metadata(
    metadata: dict[str, Any] | None,
    *,
    provider: str,
    model: str,
    status: str,
    duration_seconds: float | None = None,
    requested_model: str | None = None,
    error: str | None = None,
    temperature: float | None = None,
) -> None:
    if not isinstance(metadata, dict):
        return

    metadata["provider"] = provider
    metadata["model"] = model
    metadata["status"] = status
    if requested_model:
        metadata["requested_model"] = requested_model
    if duration_seconds is not None:
        metadata["duration_seconds"] = round(float(duration_seconds), 3)
    if error:
        metadata["error"] = error
    if temperature is not None:
        metadata["temperature"] = float(temperature)
        metadata["temperature_applied"] = False


def _compose_prompt(*, prompt: str, system_prompt: str | None) -> str:
    if not system_prompt or not system_prompt.strip():
        return prompt
    return (
        "System instructions:\n"
        f"{system_prompt.strip()}\n\n"
        "User prompt:\n"
        f"{prompt}"
    )


def _run_coroutine(coro: Any, *, timeout_seconds: float) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        result: dict[str, Any] = {}
        error: dict[str, BaseException] = {}
        done = Event()

        def _runner() -> None:
            try:
                result["value"] = asyncio.run(asyncio.wait_for(coro, timeout=timeout_seconds))
            except BaseException as exc:  # pragma: no cover - active-loop defensive bridge
                error["value"] = exc
            finally:
                done.set()

        Thread(target=_runner, daemon=True).start()
        if not done.wait(timeout=timeout_seconds + 1.0):
            raise TimeoutError(f"copilot_sdk provider timed out after {timeout_seconds:.0f}s.")
        if "value" in error:
            raise error["value"]
        return result.get("value")

    return asyncio.run(asyncio.wait_for(coro, timeout=timeout_seconds))


class CopilotSDKProvider:
    def __init__(
        self,
        *,
        timeout_seconds: float = 600.0,
        default_model: str | None = None,
        cli_path: str | None = None,
        github_token: str | None = None,
        use_logged_in_user: bool | None = None,
    ) -> None:
        self._timeout_seconds = float(timeout_seconds)
        self._default_model = str(
            default_model or os.getenv("COPILOT_SDK_MODEL") or DEFAULT_COPILOT_SDK_MODEL
        ).strip()
        if not self._default_model:
            self._default_model = DEFAULT_COPILOT_SDK_MODEL
        self._cli_path = str(cli_path or os.getenv("COPILOT_CLI_PATH") or "").strip() or None
        self._github_token = str(github_token or "").strip() or None
        self._use_logged_in_user = use_logged_in_user

    def generate(
        self,
        *,
        prompt: str,
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        metadata: dict[str, Any] | None = None,
        purpose: str = "codegen",
    ) -> str:
        user_prompt = str(prompt or "").strip()
        if not user_prompt:
            raise ValueError("copilot_sdk provider requires a non-empty prompt.")
        # C6: approve_all is only valid for codegen (F27 trust posture).
        # Any agentic reuse must replace the permission handler first —
        # track it here instead of ignoring it.
        if str(purpose or "").strip().lower() != "codegen":
            raise ValueError(
                "copilot_sdk provider refuses non-codegen purpose "
                f"{purpose!r}: PermissionHandler.approve_all must be replaced "
                "with a real permission handler before agentic reuse."
            )

        requested_model = str(model or "").strip()
        selected_model = requested_model or self._default_model
        requested_model_for_metadata = selected_model
        started_at = time.perf_counter()
        status = "running"
        resolved_model = selected_model
        error_message: str | None = None

        _apply_metadata(
            metadata,
            provider="copilot_sdk",
            model=resolved_model,
            requested_model=requested_model_for_metadata,
            status=status,
            temperature=temperature,
        )

        try:
            response_content, resolved_model = _run_coroutine(
                self._generate_async(
                    prompt=_compose_prompt(prompt=user_prompt, system_prompt=system_prompt),
                    selected_model=selected_model,
                ),
                timeout_seconds=self._timeout_seconds,
            )
            if not response_content.strip():
                raise RuntimeError("copilot_sdk provider returned an empty response.")
            status = "success"
            return response_content
        except ModuleNotFoundError as exc:
            status = "missing_sdk"
            error_message = (
                "copilot_sdk provider requires 'github-copilot-sdk'. Install it with: "
                "pip install github-copilot-sdk"
            )
            raise RuntimeError(
                error_message
            ) from exc
        except (asyncio.TimeoutError, TimeoutError) as exc:
            status = "timeout"
            error_message = f"copilot_sdk provider timed out after {self._timeout_seconds:.0f}s."
            raise RuntimeError(error_message) from exc
        except ValueError as exc:
            if "Unsupported model" in str(exc):
                status = "unsupported_model"
            error_message = str(exc)
            raise
        except Exception as exc:
            if _is_auth_error(exc):
                status = "auth_error"
                error_message = AUTH_SESSION_ERROR
                raise RuntimeError(AUTH_SESSION_ERROR) from exc
            if _is_model_error(exc):
                status = "unsupported_model"
                error_message = f"Unsupported model '{selected_model}' for provider 'copilot_sdk'."
                raise ValueError(
                    f"Unsupported model '{selected_model}' for provider 'copilot_sdk'."
                ) from exc
            status = "error"
            error_message = f"copilot_sdk provider failed: {exc}"
            raise RuntimeError(error_message) from exc
        finally:
            _apply_metadata(
                metadata,
                provider="copilot_sdk",
                model=resolved_model,
                requested_model=requested_model_for_metadata,
                status=status,
                duration_seconds=time.perf_counter() - started_at,
                error=error_message,
                temperature=temperature,
            )

    async def _generate_async(self, *, prompt: str, selected_model: str) -> tuple[str, str]:
        CopilotClient, PermissionHandler = _load_copilot_sdk()
        client_config = self._build_client_config()
        client = CopilotClient(client_config) if client_config else CopilotClient()
        session = None
        resolved_model = selected_model
        try:
            await client.start()
            available_model_ids = await self._list_available_model_ids(client)
            if available_model_ids:
                resolved_model = self._resolve_model_name(selected_model, available_model_ids)
                if resolved_model.lower() not in {item.lower() for item in available_model_ids}:
                    raise ValueError(
                        f"Unsupported model '{selected_model}' for provider 'copilot_sdk'. "
                        f"Available models: {', '.join(sorted(available_model_ids))}"
                    )

            permission_handler = getattr(PermissionHandler, "approve_all", None)
            if permission_handler is None:
                raise RuntimeError("Copilot SDK PermissionHandler.approve_all is not available.")
            # Documented trust posture — see PERMISSION_POSTURE_NOTE (F27).
            logger.debug("copilot_sdk permission posture: %s", PERMISSION_POSTURE_NOTE)

            session = await client.create_session(
                on_permission_request=permission_handler,
                model=resolved_model,
            )
            try:
                response = await session.send_and_wait(prompt, timeout=self._timeout_seconds)
            except TypeError:
                response = await session.send_and_wait(prompt)
            content = _extract_content(response)
            if not content.strip():
                raise RuntimeError("copilot_sdk provider returned an empty response.")
            return content, resolved_model
        finally:
            if session is not None and hasattr(session, "disconnect"):
                maybe_disconnect = session.disconnect()
                if asyncio.iscoroutine(maybe_disconnect):
                    await maybe_disconnect
            if hasattr(client, "stop"):
                maybe_stop = client.stop()
                if asyncio.iscoroutine(maybe_stop):
                    await maybe_stop

    def _build_client_config(self) -> dict[str, Any]:
        config: dict[str, Any] = {}
        if self._cli_path:
            config["cli_path"] = self._cli_path
        if self._github_token:
            config["github_token"] = self._github_token
            if self._use_logged_in_user is None:
                config["use_logged_in_user"] = False
        if self._use_logged_in_user is not None:
            config["use_logged_in_user"] = bool(self._use_logged_in_user)
        return config

    async def _list_available_model_ids(self, client: Any) -> set[str]:
        list_models = getattr(client, "list_models", None)
        if not callable(list_models):
            return set()

        model_payload = list_models()
        if asyncio.iscoroutine(model_payload):
            model_payload = await model_payload
        return {item for item in _extract_model_ids(model_payload) if item}

    def _resolve_model_name(self, selected_model: str, available_models: set[str]) -> str:
        direct_lookup = {value.lower(): value for value in available_models}
        selected_lower = selected_model.lower()
        if selected_lower in direct_lookup:
            return direct_lookup[selected_lower]

        alias = MODEL_ALIASES.get(selected_lower)
        if alias and alias.lower() in direct_lookup:
            return direct_lookup[alias.lower()]
        if selected_lower == "gpt-5":
            gpt5_fallbacks = (
                "gpt-5.2",
                "gpt-5.4-mini",
                "gpt-5-mini",
                "gpt-5.3-codex",
                "gpt-5.2-codex",
            )
            for fallback in gpt5_fallbacks:
                if fallback.lower() in direct_lookup:
                    return direct_lookup[fallback.lower()]
        return selected_model
