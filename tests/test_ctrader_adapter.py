import asyncio
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from live_runner.ctrader_adapter import (
    CTraderApiError,
    CTraderAuthTerminalError,
    CTraderLiveAdapter,
    _is_already_logged_in_error,
    _is_terminal_auth_error,
)


def test_is_already_logged_in_error_detects_code() -> None:
    assert _is_already_logged_in_error("ALREADY_LOGGED_IN", "")


def test_is_already_logged_in_error_detects_message() -> None:
    assert _is_already_logged_in_error("", "Account is already logged in")


def test_is_already_logged_in_error_detects_underscored_message() -> None:
    assert _is_already_logged_in_error(
        "",
        "ALREADY_LOGGED_IN: Open API application is already authorized",
    )


def test_is_terminal_auth_error_detects_not_authorized_message() -> None:
    assert _is_terminal_auth_error("", "Trading account is not authorized")


def test_is_terminal_auth_error_detects_invalid_token_code() -> None:
    assert _is_terminal_auth_error("INVALID_TOKEN", "")


def test_is_terminal_auth_error_detects_ctrader_invalid_token_code() -> None:
    assert _is_terminal_auth_error("CH_ACCESS_TOKEN_INVALID", "")


def test_is_terminal_auth_error_ignores_transient_timeout() -> None:
    assert not _is_terminal_auth_error("TIMEOUT", "Connection timeout")


def test_adapter_uses_strict_canonical_demo_switch(monkeypatch) -> None:
    monkeypatch.setenv("CTRADER_DEMO", "false")

    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
    )

    assert adapter.demo is False


def test_adapter_rejects_invalid_canonical_demo_switch(monkeypatch) -> None:
    monkeypatch.setenv("CTRADER_DEMO", "tru")

    with pytest.raises(ValueError, match="CTRADER_DEMO"):
        CTraderLiveAdapter(
            app_id="app",
            app_secret="secret",
            access_token="token",
            account_id="46736452",
        )


def test_connect_override_limits_attempts(monkeypatch) -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
        connect_max_attempts=1,
        connect_timeout_seconds=0.01,
    )
    attempts = 0

    class _Client:
        running = False

        def startService(self) -> None:
            nonlocal attempts
            attempts += 1

    monkeypatch.setattr(adapter, "_load_openapi_types", lambda: None)
    monkeypatch.setattr(adapter, "_create_client", lambda: _Client())

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(adapter.connect())

    assert attempts == 1


def test_connect_reconciliation_failure_is_retried_without_reconnect(monkeypatch) -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
        connect_max_attempts=1,
    )
    client = SimpleNamespace(running=False)

    def start_service() -> None:
        client.running = True
        adapter._on_connected(client)

    client.startService = start_service
    create_calls = 0

    def create_client():
        nonlocal create_calls
        create_calls += 1
        return client

    async def authenticate() -> None:
        adapter._authenticated = True

    async def fail_reconciliation(*, max_signals: int = 200) -> int:
        raise RuntimeError("temporary reconciliation failure")

    monkeypatch.setattr(adapter, "_load_openapi_types", lambda: None)
    monkeypatch.setattr(adapter, "_create_client", create_client)
    monkeypatch.setattr(adapter, "_authenticate", authenticate)
    monkeypatch.setattr(adapter, "reconcile_executions", fail_reconciliation)

    asyncio.run(adapter.connect())

    assert create_calls == 1
    assert adapter._connected is True
    assert adapter._authenticated is True
    assert adapter._reconciliation_required is True
    assert adapter._reconciliation_retry_at == 0.0

    async def succeed_reconciliation(*, max_signals: int = 200) -> int:
        return 0

    monkeypatch.setattr(adapter, "reconcile_executions", succeed_reconciliation)
    asyncio.run(adapter._run_pending_reconciliation(2))

    assert create_calls == 1
    assert adapter._reconciliation_required is False


def test_on_message_marks_terminal_auth_failure_for_not_authorized() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
    )
    adapter._heartbeat_payload_type = -1
    adapter._execution_payload_type = -2
    adapter._error_payload_type = 100
    adapter._order_error_payload_type = -3
    adapter._Protobuf = SimpleNamespace(
        extract=lambda _message: SimpleNamespace(
            errorCode="TRADING_ACCOUNT_IS_NOT_AUTHORIZED",
            description="Trading account is not authorized",
        )
    )

    adapter._on_message(None, SimpleNamespace(payloadType=100))

    assert adapter._terminal_auth_failure_code == "TRADING_ACCOUNT_IS_NOT_AUTHORIZED"
    assert "not authorized" in adapter._terminal_auth_failure_reason.lower()


def test_on_message_keeps_terminal_state_clear_for_already_logged_in() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
    )
    adapter._heartbeat_payload_type = -1
    adapter._execution_payload_type = -2
    adapter._error_payload_type = 101
    adapter._order_error_payload_type = -3
    adapter._Protobuf = SimpleNamespace(
        extract=lambda _message: SimpleNamespace(
            errorCode="ALREADY_LOGGED_IN",
            description="Open API application is already authorized",
        )
    )

    adapter._on_message(None, SimpleNamespace(payloadType=101))

    assert adapter._terminal_auth_failure_code == ""
    assert adapter._terminal_auth_failure_reason == ""


def test_execution_queue_overflow_requests_broker_reconciliation() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    current_client = object()
    payload = SimpleNamespace(executionType=1)
    adapter._client = current_client
    adapter._execution_payload_type = 200
    adapter._heartbeat_payload_type = -1
    adapter._Protobuf = SimpleNamespace(extract=lambda _message: payload)
    adapter._execution_queue = asyncio.Queue(maxsize=1)
    adapter._execution_queue.put_nowait(object())

    adapter._on_message(current_client, SimpleNamespace(payloadType=200))

    assert adapter._reconciliation_required is True


def test_failed_execution_event_is_requeued_and_reconciled() -> None:
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    event = object()

    assert adapter._requeue_execution_event(event) is True
    assert adapter._execution_queue.get_nowait() is event
    assert adapter._reconciliation_required is True
    assert adapter._execution_retry_counts[id(event)] == 1


def test_listener_requeues_a_failed_execution_before_retry(monkeypatch) -> None:
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    adapter._connected = True
    adapter._authenticated = True
    event = object()
    attempts = 0

    async def _persist(received):
        nonlocal attempts
        assert received is event
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary mapping failure")
        raise asyncio.CancelledError

    async def _reconcile(*, max_signals: int = 200) -> int:
        return 0

    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(adapter, "_persist_execution_event", _persist)
    monkeypatch.setattr(adapter, "reconcile_executions", _reconcile)
    monkeypatch.setattr(module.asyncio, "sleep", _sleep)
    adapter._execution_queue.put_nowait(event)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.listen_executions())

    assert attempts == 2
    assert adapter._execution_retry_counts[id(event)] == 1


def test_execution_event_requeue_is_bounded_and_defers_to_reconciliation() -> None:
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    event = object()
    for _ in range(module.EXECUTION_RETRY_LIMIT + 1):
        adapter._requeue_execution_event(event)
        while not adapter._execution_queue.empty():
            adapter._execution_queue.get_nowait()

    assert len(adapter._unresolved_execution_events) == 1
    assert adapter._unresolved_execution_events[0] is event
    assert adapter._reconciliation_required is True


def test_listener_fails_closed_when_execution_requeue_is_exhausted(monkeypatch) -> None:
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    adapter._connected = True
    adapter._authenticated = True
    event = object()
    attempts = 0

    async def _persist(_event):
        nonlocal attempts
        attempts += 1
        raise RuntimeError("temporary fill persistence failure")

    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(adapter, "_persist_execution_event", _persist)
    monkeypatch.setattr(module.asyncio, "sleep", _sleep)
    adapter._execution_queue.put_nowait(event)

    with pytest.raises(RuntimeError, match="exhausted its retry budget"):
        asyncio.run(adapter.listen_executions())

    assert attempts == module.EXECUTION_RETRY_LIMIT + 1
    assert adapter._unresolved_execution_events[-1] is event
    assert adapter._reconciliation_required is True


def test_successful_reconciliation_does_not_discard_unresolved_events(monkeypatch) -> None:
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    event = object()
    adapter._reconciliation_required = True
    adapter._unresolved_execution_events.append(event)
    adapter._execution_queue = asyncio.Queue(maxsize=1)
    adapter._execution_queue.put_nowait(object())

    async def _reconcile(*, max_signals: int = 200) -> int:
        return 0

    monkeypatch.setattr(adapter, "reconcile_executions", _reconcile)
    asyncio.run(adapter._run_pending_reconciliation(2))

    assert adapter._reconciliation_required is True
    assert list(adapter._unresolved_execution_events) == [event]


def test_stale_execution_callback_cannot_request_reconciliation() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    current_client = object()
    adapter._client = current_client
    adapter._execution_payload_type = 200
    adapter._heartbeat_payload_type = -1
    adapter._Protobuf = SimpleNamespace(extract=lambda _message: object())
    adapter._execution_queue = asyncio.Queue(maxsize=1)
    adapter._execution_queue.put_nowait(object())

    adapter._on_message(object(), SimpleNamespace(payloadType=200))

    assert adapter._reconciliation_required is False


def test_unmapped_execution_fill_is_not_acknowledged_as_persisted(monkeypatch) -> None:
    from live_runner import ctrader_adapter as module

    adapter = module.CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    adapter._ProtoOAExecutionType = SimpleNamespace(ORDER_FILLED=1, ORDER_PARTIAL_FILL=2)
    event = SimpleNamespace(
        executionType=1,
        order=SimpleNamespace(orderId=45678, clientOrderId=""),
        deal=None,
        HasField=lambda field: field == "order",
    )
    monkeypatch.setattr(module, "find_live_signal_id_by_broker_order_id", lambda *_args: None)

    with pytest.raises(ValueError, match="could not be mapped"):
        asyncio.run(adapter._persist_execution_event(event))


def test_listener_keeps_processing_queue_when_reconciliation_fails(monkeypatch) -> None:
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    adapter._connected = True
    adapter._authenticated = True
    adapter._reconciliation_required = True
    event = object()
    attempts = 0
    persisted: list[object] = []

    async def reconcile_executions(*, max_signals: int = 200) -> int:
        nonlocal attempts
        assert max_signals == 200
        attempts += 1
        raise RuntimeError("temporary broker failure")

    async def persist_execution(received: object) -> None:
        persisted.append(received)
        raise asyncio.CancelledError

    adapter._execution_queue.put_nowait(event)
    monkeypatch.setattr(adapter, "reconcile_executions", reconcile_executions)
    monkeypatch.setattr(adapter, "_persist_execution_event", persist_execution)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.listen_executions())

    assert attempts == 1
    assert persisted == [event]
    assert adapter._reconciliation_required is True
    assert adapter._reconciliation_retry_at > 0


def test_listener_clears_reconciliation_flag_after_success(monkeypatch) -> None:
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    adapter._connected = True
    adapter._authenticated = True
    adapter._reconciliation_required = True
    event = object()
    attempts = 0

    async def reconcile_executions(*, max_signals: int = 200) -> int:
        nonlocal attempts
        assert max_signals == 200
        attempts += 1
        return 0

    async def persist_execution(_event: object) -> None:
        raise asyncio.CancelledError

    adapter._execution_queue.put_nowait(event)
    monkeypatch.setattr(adapter, "reconcile_executions", reconcile_executions)
    monkeypatch.setattr(adapter, "_persist_execution_event", persist_execution)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(adapter.listen_executions())

    assert attempts == 1
    assert adapter._reconciliation_required is False
    assert adapter._reconciliation_retry_at == 0.0


def test_write_env_key_uses_portable_lock_around_atomic_replace(monkeypatch, tmp_path) -> None:
    from live_runner import ctrader_adapter as module

    env_path = tmp_path / ".accounts.env"
    env_path.write_text("CTRADER_ACCESS_TOKEN=old\nUNCHANGED=value\n", encoding="utf-8")
    events: list[str] = []
    replacement: tuple[Path, Path] | None = None
    active_lock = None
    real_replace = module.os.replace

    class _TrackingLock:
        def __init__(self, lock_file: str) -> None:
            self.path = Path(lock_file)
            self.active = False

        def __enter__(self):
            self.active = True
            events.append("lock-enter")
            return self

        def __exit__(self, *_args) -> None:
            self.active = False
            events.append("lock-exit")

    @contextmanager
    def tracking_lock(handle):
        nonlocal active_lock
        active_lock = _TrackingLock(handle.name)
        active_lock.active = True
        events.append("lock-enter")
        try:
            yield
        finally:
            active_lock.active = False
            events.append("lock-exit")

    def tracked_replace(source: Path, destination: Path) -> None:
        nonlocal replacement
        assert active_lock is not None
        assert active_lock.active
        replacement = (Path(source), Path(destination))
        events.append("replace")
        real_replace(source, destination)

    monkeypatch.setattr(module, "_exclusive_file_lock", tracking_lock)
    monkeypatch.setattr(module.os, "replace", tracked_replace)

    module._write_env_key(env_path, "CTRADER_ACCESS_TOKEN", "new")

    assert active_lock is not None
    assert active_lock.path == env_path.with_name(f"{env_path.name}.lock")
    assert events == ["lock-enter", "replace", "lock-exit"]
    assert replacement is not None
    source, destination = replacement
    assert source.parent == env_path.parent
    assert source.name.startswith(f".{env_path.name}.")
    assert source.suffix == ".tmp"
    assert destination == env_path.resolve()
    assert env_path.read_text(encoding="utf-8") == (
        "CTRADER_ACCESS_TOKEN=new\nUNCHANGED=value\n"
    )


def test_refresh_access_token_updates_runtime_and_dotenv(tmp_path) -> None:
    env_path = tmp_path / ".accounts.env"
    env_path.write_text(
        "CTRADER_ACCESS_TOKEN=old\nCTRADER_REFRESH_TOKEN=old_refresh\n",
        encoding="utf-8",
    )
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="old",
        refresh_token="old_refresh",
        account_id="46736452",
        accounts_env_path=env_path,
    )

    class _RefreshReq:
        def __init__(self) -> None:
            self.refreshToken = ""

    class _RefreshRes:
        accessToken = "new_access"
        refreshToken = "new_refresh"
        expiresIn = 2628000

    async def _fake_send(request, **_kwargs):
        assert isinstance(request, _RefreshReq)
        assert request.refreshToken == "old_refresh"
        return _RefreshRes()

    adapter._ProtoOARefreshTokenReq = _RefreshReq
    adapter._ProtoOARefreshTokenRes = _RefreshRes
    adapter._send_request = _fake_send

    assert asyncio.run(adapter._refresh_access_token()) is True
    assert adapter.access_token == "new_access"
    assert adapter.refresh_token == "new_refresh"
    contents = env_path.read_text(encoding="utf-8")
    assert "CTRADER_ACCESS_TOKEN=new_access" in contents
    assert "CTRADER_REFRESH_TOKEN=new_refresh" in contents


def test_stale_client_callbacks_cannot_change_current_connection_state() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    current_client = object()
    stale_client = object()
    adapter._client = current_client

    adapter._on_connected(stale_client)
    assert adapter._connected is False
    assert adapter._connected_event.is_set() is False

    adapter._connected = True
    adapter._authenticated = True
    adapter._connected_event.set()
    adapter._on_disconnected(stale_client, "late disconnect")
    assert adapter._connected is True
    assert adapter._authenticated is True
    assert adapter._connected_event.is_set() is True


def test_send_request_with_refresh_retry_retries_once_after_token_refresh() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="old",
        refresh_token="refresh",
        account_id="46736452",
    )
    calls: list[object] = []
    request = object()
    response = object()

    async def _fake_send(sent_request, **_kwargs):
        calls.append(sent_request)
        if len(calls) == 1:
            raise CTraderApiError("CH_ACCESS_TOKEN_INVALID", "expired")
        return response

    async def _fake_refresh(**_kwargs):
        adapter.access_token = "new"
        return True

    adapter._send_request = _fake_send
    adapter._refresh_access_token = _fake_refresh

    assert asyncio.run(adapter._send_request_with_refresh_retry(request)) is response
    assert calls == [request, request]


def test_delayed_old_token_error_does_not_rotate_refresh_token_twice(tmp_path) -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="old_access",
        refresh_token="old_refresh",
        account_id="46736452",
        accounts_env_path=tmp_path / ".accounts.env",
    )
    both_requests_started = asyncio.Event()
    first_refresh_complete = asyncio.Event()
    request_attempts: dict[object, int] = {}
    first_attempts_started = 0
    class RefreshRequest:
        def __init__(self) -> None:
            self.refreshToken = ""

    class RefreshResponse:
        accessToken = "new_access"
        refreshToken = "new_refresh"
        expiresIn = 2628000

    refresh_requests: list[str] = []
    request_a = object()
    request_b = object()

    async def send_request(request, **_kwargs):
        nonlocal first_attempts_started
        if isinstance(request, RefreshRequest):
            refresh_requests.append(request.refreshToken)
            first_refresh_complete.set()
            return RefreshResponse()
        attempt = request_attempts.get(request, 0) + 1
        request_attempts[request] = attempt
        if attempt > 1:
            return request
        first_attempts_started += 1
        if first_attempts_started == 2:
            both_requests_started.set()
        await both_requests_started.wait()
        if request is request_a:
            raise CTraderApiError("CH_ACCESS_TOKEN_INVALID", "expired old token")
        await first_refresh_complete.wait()
        raise CTraderApiError("CH_ACCESS_TOKEN_INVALID", "delayed old token error")

    adapter._ProtoOARefreshTokenReq = RefreshRequest
    adapter._ProtoOARefreshTokenRes = RefreshResponse
    adapter._send_request = send_request

    async def run_requests():
        return await asyncio.gather(
            adapter._send_request_with_refresh_retry(request_a),
            adapter._send_request_with_refresh_retry(request_b),
        )

    assert asyncio.run(run_requests()) == [request_a, request_b]
    assert refresh_requests == ["old_refresh"]


def test_waiter_does_not_treat_failed_token_refresh_as_success(tmp_path) -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="old_access",
        refresh_token="old_refresh",
        account_id="46736452",
        accounts_env_path=tmp_path / ".accounts.env",
    )

    class RefreshRequest:
        def __init__(self) -> None:
            self.refreshToken = ""

    class InvalidRefreshResponse:
        accessToken = ""
        refreshToken = ""

    refresh_attempts = 0

    async def send_request(request, **_kwargs):
        nonlocal refresh_attempts
        if isinstance(request, RefreshRequest):
            refresh_attempts += 1
            return InvalidRefreshResponse()
        raise AssertionError("Only refresh requests are expected")

    adapter._ProtoOARefreshTokenReq = RefreshRequest
    adapter._ProtoOARefreshTokenRes = InvalidRefreshResponse
    adapter._send_request = send_request

    async def refresh_twice():
        return await asyncio.gather(
            adapter._refresh_access_token(failed_access_token="old_access"),
            adapter._refresh_access_token(failed_access_token="old_access"),
        )

    assert asyncio.run(refresh_twice()) == [False, False]
    assert refresh_attempts == 2
    assert adapter.access_token == "old_access"
    assert adapter.refresh_token == "old_refresh"


def test_auth_refresh_rebuilds_account_auth_request_with_new_access_token() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="old_access",
        refresh_token="refresh",
        account_id="46736452",
    )
    sent_tokens: list[str] = []

    class _AccountAuthReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0
            self.accessToken = ""

    class _AccountAuthRes:
        pass

    adapter._ProtoOAAccountAuthReq = _AccountAuthReq
    adapter._ProtoOAAccountAuthRes = _AccountAuthRes

    async def _fake_send(request, **_kwargs):
        sent_tokens.append(request.accessToken)
        if len(sent_tokens) == 1:
            raise CTraderApiError("CH_ACCESS_TOKEN_INVALID", "expired")
        return _AccountAuthRes()

    async def _fake_refresh():
        adapter.access_token = "new_access"
        return True

    adapter._send_request = _fake_send
    adapter._refresh_access_token = _fake_refresh

    async def _run() -> object:
        try:
            return await adapter._send_account_auth_request(timeout_seconds=15)
        except CTraderApiError as exc:
            assert _is_terminal_auth_error(exc.error_code, str(exc))
            assert await adapter._refresh_access_token()
            return await adapter._send_account_auth_request(timeout_seconds=15)

    assert isinstance(asyncio.run(_run()), _AccountAuthRes)
    assert sent_tokens == ["old_access", "new_access"]


def test_connect_cancellation_tears_down_started_client(monkeypatch) -> None:
    async def _run() -> tuple[CTraderLiveAdapter, int]:
        adapter = CTraderLiveAdapter(
            app_id="app",
            app_secret="secret",
            access_token="token",
            account_id="46736452",
            connect_max_attempts=1,
            connect_timeout_seconds=60,
        )
        client_started = asyncio.Event()
        stop_calls = 0

        class _Client:
            running = False

            def startService(self) -> None:
                self.running = True
                client_started.set()

            def stopService(self) -> None:
                nonlocal stop_calls
                stop_calls += 1
                self.running = False

        monkeypatch.setattr(adapter, "_load_openapi_types", lambda: None)
        monkeypatch.setattr(adapter, "_create_client", lambda: _Client())

        connect_task = asyncio.create_task(adapter.connect())
        await asyncio.wait_for(client_started.wait(), timeout=0.1)
        connect_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await connect_task
        return adapter, stop_calls

    adapter, stop_calls = asyncio.run(_run())

    assert stop_calls == 1
    assert adapter._client is None
    assert adapter._connected is False
    assert adapter._authenticated is False
    assert adapter._connected_event.is_set() is False


def test_disconnect_times_out_instead_of_hanging() -> None:
    async def _run() -> CTraderLiveAdapter:
        adapter = CTraderLiveAdapter(
            app_id="app",
            app_secret="secret",
            access_token="token",
            account_id="46736452",
        )
        adapter._loop = asyncio.get_running_loop()
        adapter._connected = True
        adapter._authenticated = True

        class _NeverCompletes:
            def asFuture(self, loop):
                return loop.create_future()

        class _Client:
            running = True

            def stopService(self):
                return _NeverCompletes()

        adapter._client = _Client()
        await adapter.disconnect()
        return adapter

    adapter = asyncio.run(_run())

    assert adapter._client is None
    assert adapter._connected is False
    assert adapter._authenticated is False


def test_get_balance_marks_terminal_auth_failure_for_not_authorized() -> None:
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="46736452",
    )
    adapter._connected = True
    adapter._authenticated = True

    class _TraderReq:
        def __init__(self) -> None:
            self.ctidTraderAccountId = 0

    adapter._ProtoOATraderReq = _TraderReq
    adapter._ProtoOATraderRes = type("_TraderRes", (), {})

    async def _raise_auth_error(*_args, **_kwargs):
        raise CTraderApiError(
            error_code="TRADING_ACCOUNT_IS_NOT_AUTHORIZED",
            description="Trading account is not authorized",
        )

    adapter._send_request = _raise_auth_error

    with pytest.raises(CTraderAuthTerminalError):
        asyncio.run(adapter.get_balance())

    assert adapter._terminal_auth_failure_code == "TRADING_ACCOUNT_IS_NOT_AUTHORIZED"
    assert "not authorized" in adapter._terminal_auth_failure_reason.lower()


# ── Fill identity: two real fills must not collapse onto one dedupe key ──────────────────────
#
# The adapter used to derive the key from `(order, timestamp, volume)` whenever the broker sent
# no deal id. Two genuine equal-sized fills on one order therefore produced the *same* key, and
# the second was logged as a duplicate and dropped - the position ended up short with a log line
# arguing against it. These tests drive the real persistence path against a throwaway database,
# because the defect only exists once the key reaches the unique index.


def _seed_signal(database, signal_id: str, *, broker_order_id: str = "45678") -> None:
    from datetime import datetime, timezone

    with database.get_session() as session:
        session.add(database.LiveAccountModel(id="acc-1", broker="ctrader", name="acc-1"))
        session.add(
            database.LiveDeploymentModel(
                id="acc-1",
                strategy_name="fill-identity",
                account_id="acc-1",
                symbols_json=["EURUSD"],
                timeframe="1m",
            )
        )
        session.add(
            database.LiveSignalModel(
                id=signal_id,
                deployment_id="acc-1",
                symbol="EURUSD",
                action="BUY",
                theoretical_price=1.2345,
                source="internal",
                broker_order_id=broker_order_id,
                timestamp=datetime.now(timezone.utc),
            )
        )
        session.commit()


def _execution_adapter():
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="46736452"
    )
    adapter.account_config_id = "acc-1"
    adapter._ProtoOAExecutionType = SimpleNamespace(ORDER_FILLED=1, ORDER_PARTIAL_FILL=2)
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._order_id_to_symbol[45678] = "EURUSD"
    return adapter


def _fill_event(
    *,
    signal_id: str,
    deal_id: int,
    volume: int = 500_000,
    timestamp: int = 1_700_000_000_000,
):
    """A fill whose deal carries the broker's per-fill id."""
    return SimpleNamespace(
        executionType=1,
        order=None,
        deal=SimpleNamespace(
            dealId=deal_id,
            orderId=45678,
            symbolId=1,
            filledVolume=volume,
            executionPrice=1.2345,
            executionTimestamp=timestamp,
            commission=0.0,
        ),
        HasField=lambda field: field == "deal",
    )


def _order_fill_event(*, signal_id: str, executed_volume: int, timestamp: int = 1_700_000_000_000):
    """An order-level fill: no deal, so the only quantity is the order's cumulative volume."""
    return SimpleNamespace(
        executionType=2,
        order=SimpleNamespace(
            orderId=45678,
            clientOrderId=signal_id,
            executedVolume=executed_volume,
            utcLastUpdateTimestamp=timestamp,
        ),
        deal=None,
        HasField=lambda field: field == "order",
    )


def _rows(database) -> list[tuple[str | None, int | None, float]]:
    import sqlite3

    conn = sqlite3.connect(f"file:{database.DB_PATH}?mode=ro", uri=True)
    try:
        return list(
            conn.execute(
                "SELECT dedupe_key, broker_fill_id, filled_qty FROM live_executions ORDER BY rowid"
            )
        )
    finally:
        conn.close()


def test_two_distinct_fills_with_equal_size_and_timestamp_are_two_rows(isolated_db) -> None:
    """**The blocker.** Equal size, same millisecond, one order - only the broker id separates them.

    Both fills must be stored. Before the fix the second one's key collided with the first and
    `save_live_execution` refused it as a duplicate that it was not.
    """
    database = isolated_db[0]
    signal_id = "11111111-1111-4111-8111-111111111111"
    _seed_signal(database, signal_id)
    adapter = _execution_adapter()

    asyncio.run(adapter._persist_execution_event(_fill_event(signal_id=signal_id, deal_id=1001)))
    asyncio.run(adapter._persist_execution_event(_fill_event(signal_id=signal_id, deal_id=1002)))

    rows = _rows(database)
    assert [row[0] for row in rows] == ["ctrader:deal:1001", "ctrader:deal:1002"], rows
    assert [row[1] for row in rows] == [1001, 1002], rows
    assert sum(row[2] for row in rows) == pytest.approx(1000.0)


def test_a_redelivered_fill_is_stored_once(isolated_db) -> None:
    """The other half: the same fill arriving twice must not be counted twice."""
    database = isolated_db[0]
    signal_id = "22222222-2222-4222-8222-222222222222"
    _seed_signal(database, signal_id)
    adapter = _execution_adapter()

    event = _fill_event(signal_id=signal_id, deal_id=1003)
    asyncio.run(adapter._persist_execution_event(event))
    asyncio.run(adapter._persist_execution_event(event))

    rows = _rows(database)
    assert len(rows) == 1, rows
    assert rows[0][1] == 1003
    assert rows[0][2] == pytest.approx(500.0)


def test_an_order_level_fill_is_identified_by_the_cumulative_volume(isolated_db) -> None:
    """With no deal id, identity comes from the order's cumulative executed volume.

    That number strictly increases per fill, so two genuine fills are distinguishable, and the
    persistence cap keeps the second row at the increment rather than re-storing the total.
    """
    database = isolated_db[0]
    signal_id = "33333333-3333-4333-8333-333333333333"
    _seed_signal(database, signal_id)
    adapter = _execution_adapter()

    asyncio.run(
        adapter._persist_execution_event(
            _order_fill_event(signal_id=signal_id, executed_volume=500_000)
        )
    )
    asyncio.run(
        adapter._persist_execution_event(
            _order_fill_event(signal_id=signal_id, executed_volume=1_000_000)
        )
    )

    rows = _rows(database)
    assert len(rows) == 2, rows
    assert rows[0][0] == "ctrader:order:45678:cum:500000"
    assert rows[1][0] == "ctrader:order:45678:cum:1000000"
    assert [row[2] for row in rows] == [pytest.approx(500.0), pytest.approx(500.0)], (
        "the order's executedVolume is cumulative; storing it whole would double-count the "
        "first fill"
    )
    assert sum(row[2] for row in rows) == pytest.approx(1000.0)


def test_a_redelivered_order_level_fill_adds_nothing(isolated_db) -> None:
    database = isolated_db[0]
    signal_id = "44444444-4444-4444-8444-444444444444"
    _seed_signal(database, signal_id)
    adapter = _execution_adapter()

    event = _order_fill_event(signal_id=signal_id, executed_volume=500_000)
    asyncio.run(adapter._persist_execution_event(event))
    asyncio.run(adapter._persist_execution_event(event))

    rows = _rows(database)
    assert len(rows) == 1, rows
    assert rows[0][2] == pytest.approx(500.0)


def test_a_fill_with_no_identity_at_all_is_refused_not_collided() -> None:
    """No deal id, no cumulative volume, no timestamp: the key cannot separate two fills.

    Inventing one would either lose a real fill (a colliding key) or double-count a redelivery
    (a unique key per event). The event is refused so it is retained for reconciliation, which
    reads deals that do carry ids.
    """
    with pytest.raises(ValueError, match="no deal id, no cumulative order volume and no timestamp"):
        CTraderLiveAdapter._build_dedupe_key(
            SimpleNamespace(dealId=0),
            order_id=123,
            timestamp_ms=0,
            filled_volume=500.0,
            order_cumulative_volume=0.0,
        )


def test_the_fallback_key_separates_every_component_it_carries() -> None:
    """A timestamp-only fallback still has to separate two fills that differ in any component."""
    base = CTraderLiveAdapter._build_dedupe_key(
        SimpleNamespace(dealId=0),
        order_id=123,
        timestamp_ms=1000,
        filled_volume=500.0,
    )
    assert base == "ctrader:order:123:1000:500"
    for changed in (
        CTraderLiveAdapter._build_dedupe_key(
            SimpleNamespace(dealId=0), order_id=124, timestamp_ms=1000, filled_volume=500.0
        ),
        CTraderLiveAdapter._build_dedupe_key(
            SimpleNamespace(dealId=0), order_id=123, timestamp_ms=2000, filled_volume=500.0
        ),
        CTraderLiveAdapter._build_dedupe_key(
            SimpleNamespace(dealId=0), order_id=123, timestamp_ms=1000, filled_volume=501.0
        ),
    ):
        assert changed != base, f"a distinct fill produced the same key {base}"


def test_a_deal_that_grows_is_amended_not_dropped(isolated_db) -> None:
    """A dealId repeated with a larger quantity is the broker amending the fill.

    `ProtoOADeal.filledVolume` can grow for a deal already reported. Dropping the repeat as a
    duplicate freezes the stored quantity at its first-seen value and loses the rest of the
    fill, which is what the plain insert path did.
    """
    database = isolated_db[0]
    signal_id = "55555555-5555-4555-8555-555555555555"
    _seed_signal(database, signal_id)
    adapter = _execution_adapter()

    event = SimpleNamespace(
        executionType=1,
        order=SimpleNamespace(
            orderId=45678,
            clientOrderId=signal_id,
            executedVolume=500_000,
            utcLastUpdateTimestamp=1_700_000_000_000,
        ),
        deal=SimpleNamespace(
            dealId=2001,
            orderId=45678,
            symbolId=1,
            filledVolume=300_000,
            executionPrice=1.2345,
            executionTimestamp=1_700_000_000_000,
            commission=0.0,
        ),
        HasField=lambda field: field == "order",
    )
    amended = SimpleNamespace(**{**event.__dict__})
    amended.deal = SimpleNamespace(
        dealId=2001,
        orderId=45678,
        symbolId=1,
        filledVolume=500_000,
        executionPrice=1.2345,
        executionTimestamp=1_700_000_000_000,
        commission=0.0,
    )

    asyncio.run(adapter._persist_execution_event(event))
    asyncio.run(adapter._persist_execution_event(amended))

    rows = _rows(database)
    assert len(rows) == 1, rows
    assert rows[0][0] == "ctrader:deal:2001"
    assert rows[0][2] == pytest.approx(500.0), (
        "the amendment was dropped, so the stored quantity is stuck at its first-seen value"
    )


# ── Execution price: 0.0 is not a price ──────────────────────────────────────────────────────
#
# `_resolve_execution_price` returns 0.0 as its "could not be read" sentinel, so an omitted
# price, an explicitly-sent zero and a genuine zero all stored the same value. The classifier
# below is what makes the three countable, and it is persisted beside the price.


def _deal_with_price(price: float | None):
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOADeal

    deal = ProtoOADeal()
    if price is not None:
        deal.executionPrice = price
    return deal


def test_a_real_price_is_observed() -> None:
    deal = _deal_with_price(261.7)

    assert CTraderLiveAdapter._execution_price_presence(order=None, deal=deal) == "observed"
    assert CTraderLiveAdapter._resolve_execution_price(order=None, deal=deal) == pytest.approx(
        261.7
    )


def test_a_price_of_exactly_zero_is_sent_but_unusable() -> None:
    """The case that looks like a price. `0.0` must never be classified as observed."""
    deal = _deal_with_price(0.0)

    assert deal.HasField("executionPrice") is True
    assert CTraderLiveAdapter._execution_price_presence(order=None, deal=deal) == "sent_bad"
    assert CTraderLiveAdapter._resolve_execution_price(order=None, deal=deal) == 0.0


def test_an_omitted_price_is_absent_not_zero() -> None:
    """`optional double` carries presence, so an unset field is distinguishable from a sent 0."""
    deal = _deal_with_price(None)

    assert deal.HasField("executionPrice") is False
    assert CTraderLiveAdapter._execution_price_presence(order=None, deal=deal) == "absent"


@pytest.mark.parametrize("price", [-1.0, float("nan"), float("inf")])
def test_an_unusable_price_is_never_returned_as_the_price(price: float) -> None:
    """A negative or non-finite price used to pass `if value:` - NaN did, and poisoned averages."""
    deal = _deal_with_price(price)

    assert CTraderLiveAdapter._execution_price_presence(order=None, deal=deal) == "sent_bad"
    assert CTraderLiveAdapter._resolve_execution_price(order=None, deal=deal) == 0.0


def test_the_deal_price_wins_over_the_order_price() -> None:
    """Precedence, and the verdict follows the source that answered."""
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOAOrder

    order = ProtoOAOrder()
    order.executionPrice = 1.0
    deal = _deal_with_price(2.0)

    assert CTraderLiveAdapter._resolve_execution_price(order=order, deal=deal) == pytest.approx(
        2.0
    )
    assert CTraderLiveAdapter._execution_price_presence(order=order, deal=deal) == "observed"


def test_the_verdict_is_persisted_beside_the_price(isolated_db) -> None:
    """The column is only worth having if the fill path writes it."""
    database = isolated_db[0]
    signal_id = "66666666-6666-4666-8666-666666666666"
    _seed_signal(database, signal_id)
    adapter = _execution_adapter()

    def _fill(deal_id: int, price: float | None):
        from ctrader_open_api.messages.OpenApiModelMessages_pb2 import ProtoOADeal

        deal = ProtoOADeal()
        deal.dealId = deal_id
        deal.orderId = 45678
        deal.symbolId = 1
        deal.filledVolume = 500_000
        deal.executionTimestamp = 1_700_000_000_000
        if price is not None:
            deal.executionPrice = price
        return SimpleNamespace(
            executionType=1,
            order=None,
            deal=deal,
            HasField=lambda field: field == "deal",
        )

    asyncio.run(adapter._persist_execution_event(_fill(3001, 261.7)))
    asyncio.run(adapter._persist_execution_event(_fill(3002, 0.0)))
    asyncio.run(adapter._persist_execution_event(_fill(3003, None)))

    import sqlite3

    conn = sqlite3.connect(f"file:{database.DB_PATH}?mode=ro", uri=True)
    try:
        rows = list(
            conn.execute(
                "SELECT broker_fill_id, actual_price, actual_price_observed "
                "FROM live_executions ORDER BY rowid"
            )
        )
    finally:
        conn.close()

    assert rows == [(3001, 261.7, "observed"), (3002, 0.0, "sent_bad"), (3003, 0.0, "absent")], (
        rows
    )


def test_the_column_is_additive_nullable_and_has_no_default(isolated_db) -> None:
    """No verdict is not a verdict of `observed`, so the column must be nullable and defaultless."""
    import sqlite3

    database = isolated_db[0]
    conn = sqlite3.connect(f"file:{database.DB_PATH}?mode=ro", uri=True)
    try:
        columns = {row[1]: row for row in conn.execute("PRAGMA table_info(live_executions)")}
    finally:
        conn.close()

    assert "actual_price_observed" in columns, (
        "live_executions has no actual_price_observed column, so a 0.0 price stays "
        "indistinguishable from an omitted one"
    )
    notnull, default = columns["actual_price_observed"][3], columns["actual_price_observed"][4]
    assert notnull == 0, "existing rows have no verdict and must be allowed to stay NULL"
    assert default is None, (
        "a database default would fabricate a verdict for rows nobody classified"
    )


def test_the_migration_adds_the_column_and_is_idempotent(isolated_db) -> None:
    """Staged by dropping the column, so this exercises the ALTER rather than `create_all`."""
    import sqlite3

    database = isolated_db[0]
    db_path = database.DB_PATH
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("ALTER TABLE live_executions DROP COLUMN actual_price_observed")
        conn.commit()
        missing = "actual_price_observed" not in {
            row[1] for row in conn.execute("PRAGMA table_info(live_executions)")
        }
    finally:
        conn.close()
    assert missing, "could not stage a table without the column, so this proves nothing"

    with database.get_engine().connect() as writable:
        database._add_live_executions_actual_price_observed(writable)
        writable.commit()
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        after = {row[1] for row in conn.execute("PRAGMA table_info(live_executions)")}
    finally:
        conn.close()
    assert "actual_price_observed" in after, "the migration did not add the column"

    with database.get_engine().connect() as writable:
        database._add_live_executions_actual_price_observed(writable)
        writable.commit()


# ── Connection auth state ────────────────────────────────────────────────────────────────────


def test_a_new_connection_cannot_inherit_authentication() -> None:
    """A connect event means a socket that has not been authenticated yet.

    `_authenticate` short-circuits on `_authenticated and _symbol_name_to_id`, and a dozen gates
    - including the order paths - test `not self._authenticated`. A stale True plus a populated
    symbol cache would let a trade go out on a connection the broker never authenticated.
    """
    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    client = object()
    adapter._client = client
    adapter._connected = True
    adapter._authenticated = True
    adapter._symbol_name_to_id = {"EURUSD": 1}
    adapter._already_logged_in_notified = True

    adapter._on_connected(client)

    assert adapter._connected is True, "the connection is real; only the auth state is stale"
    assert adapter._authenticated is False
    assert adapter._already_logged_in_notified is False


def test_a_blanked_demo_switch_is_refused_not_defaulted() -> None:
    """`resolve_env_value` keeps "blanked" distinct from "absent"; the adapter must too.

    With `default=False` the same blank would select live trading silently.
    """
    with pytest.raises(ValueError, match="CTRADER_DEMO"):
        CTraderLiveAdapter(
            app_id="app",
            app_secret="secret",
            access_token="token",
            account_id="1",
            env_values={"CTRADER_DEMO": ""},
        )


def test_an_absent_demo_switch_still_defaults_to_demo(monkeypatch) -> None:
    monkeypatch.delenv("CTRADER_DEMO", raising=False)

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1", env_values={}
    )

    assert adapter.demo is True


def test_an_explicit_demo_argument_beats_the_environment(monkeypatch) -> None:
    monkeypatch.setenv("CTRADER_DEMO", "true")

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1", demo=False
    )

    assert adapter.demo is False


# ── Connect-failure classification ──────────────────────────────────────────────────────────


def test_a_deferred_response_timeout_is_not_reported_as_authentication(caplog) -> None:
    """`(15, 'Deferred')` is Twisted's response timeout, not a credential problem.

    It contains neither "token" nor "timeout", so it used to fall into the fallback that named
    authentication - once per retry, forever, for a token that worked.
    """
    import logging

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )
    exc = Exception(15, "Deferred")

    with caplog.at_level(logging.INFO, logger="live-runner"):
        adapter._log_connect_failure(exc, str(exc), str(exc).lower())

    messages = [record.getMessage() for record in caplog.records]
    assert any("NO RESPONSE" in message and "15s" in message for message in messages), messages
    assert not any("authentication" in message.lower() for message in messages), (
        "a response timeout was reported as an authentication problem"
    )


def test_a_similar_message_is_not_claimed_as_a_response_timeout() -> None:
    from live_runner.ctrader_adapter import _classify_deferred_response_timeout

    assert _classify_deferred_response_timeout(Exception(), "(15, 'Deferred')") == (True, 15)
    assert _classify_deferred_response_timeout(Exception(), "(15, 'Other')") == (False, None)
    assert _classify_deferred_response_timeout(Exception(), "error 15 Deferred") == (False, None)


def test_an_unclassified_failure_does_not_name_a_cause(caplog) -> None:
    import logging

    adapter = CTraderLiveAdapter(
        app_id="app", app_secret="secret", access_token="token", account_id="1"
    )

    with caplog.at_level(logging.INFO, logger="live-runner"):
        adapter._log_connect_failure(RuntimeError("x"), "something new", "something new")

    joined = " ".join(record.getMessage() for record in caplog.records)
    assert "NOT CLASSIFIED" in joined
    assert "auth" not in joined.lower().replace("not classified", ""), (
        "the fallback guessed a cause, which is what sent operators to refresh a working token"
    )


def test_an_unreadable_cumulative_volume_is_not_treated_as_a_quantity() -> None:
    """`executedVolume` feeds the persistence cap and the dedupe key, so only a real fill counts.

    An unset protobuf field reads back as `0.0`, and a broker bug could send a negative or a
    NaN. Returning those verbatim would let a `0` cumulative produce the key
    `...:cum:0` and hand `save_live_execution_up_to_cumulative` a cap of zero - both are
    "no readable quantity" and must collapse to `0.0` so the caller takes its no-cumulative path.
    """
    assert CTraderLiveAdapter._resolve_order_cumulative_volume(None) == 0.0
    assert CTraderLiveAdapter._resolve_order_cumulative_volume(SimpleNamespace()) == 0.0
    assert (
        CTraderLiveAdapter._resolve_order_cumulative_volume(
            SimpleNamespace(executedVolume=0)
        )
        == 0.0
    )
    assert (
        CTraderLiveAdapter._resolve_order_cumulative_volume(
            SimpleNamespace(executedVolume=-500_000)
        )
        == 0.0
    )
    assert (
        CTraderLiveAdapter._resolve_order_cumulative_volume(
            SimpleNamespace(executedVolume=float("nan"))
        )
        == 0.0
    )
    assert (
        CTraderLiveAdapter._resolve_order_cumulative_volume(
            SimpleNamespace(executedVolume=float("inf"))
        )
        == 0.0
    )
    assert (
        CTraderLiveAdapter._resolve_order_cumulative_volume(
            SimpleNamespace(executedVolume="not-a-number")
        )
        == 0.0
    )
    assert (
        CTraderLiveAdapter._resolve_order_cumulative_volume(
            SimpleNamespace(executedVolume=500_000)
        )
        == 500_000.0
    )
