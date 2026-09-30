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


def test_a_new_connection_cannot_inherit_authentication_from_the_previous_one() -> None:
    """A connect event means a socket that has not been authenticated yet.

    `_on_disconnected` clears `_authenticated`, and `_teardown_client` clears it
    too, so the ordinary paths leave nothing stale. What neither covers is a
    transport that reports a fresh connection on the same client without a
    disconnect in between - an internal reconnect, or a dropped event.

    That case matters because of what `_authenticate` does with the flag:

        if self._authenticated and self._symbol_name_to_id:
            return

    A stale True plus a populated symbol cache returns before any authentication
    request is sent. The adapter would then satisfy every `if not self._connected
    or not self._authenticated` gate - a dozen of them, including the order paths -
    on a connection the broker never authenticated.

    The fix belongs in `_on_connected` rather than in the callers: establishing a
    connection and being authenticated on it are separate facts, and a connect
    callback is the only place that learns the first one has just become true.
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
    assert adapter._authenticated is False, (
        "a new connection must not inherit authentication from the previous one - "
        "_authenticate returns early on a stale True, so every downstream gate "
        "would pass on a socket the broker never authenticated"
    )
    assert adapter._already_logged_in_notified is False, (
        "the already-logged-in notice is per-connection too; a new session should "
        "be free to log the fact again"
    )


def test_a_blanked_ctrader_demo_value_is_refused_rather_than_defaulted(monkeypatch) -> None:
    """An empty `CTRADER_DEMO` means someone blanked it, not that it was never set.

    `live_runner/credential_env.resolve_env_value` is explicit about this: a key
    defined in `.accounts.env` wins "even when its value is empty
    (present-but-empty stays empty)". Preserving that distinction is the whole
    point of the function, and it is what lets an operator blank a value on purpose.

    The adapter then discarded it with `resolve_env_value(...) or None`, which turns
    every falsy value into "absent" and hands the parser a `None` - so the strict
    parse never sees the empty string and the default is applied instead. The other
    two readers of the same variable disagree: `preflight` and `daemon` pass the
    value through and both raise on `''`.

    The consequences are not hypothetical. An operator who blanks `CTRADER_DEMO`
    gets a preflight that refuses to start and an adapter that would quietly
    connect as demo. The adapter is the thing that opens the connection, so it is
    the one whose opinion counts - and the divergence sits one token away from the
    worst outcome in this codebase: change `default=True` to `default=False` and
    the same empty value selects *live* trading, silently, with no error anywhere.

    So the empty value has to reach the parser, which rejects it.
    """
    monkeypatch.delenv("CTRADER_DEMO", raising=False)
    env_values = {"CTRADER_DEMO": ""}

    with pytest.raises(ValueError) as excinfo:
        CTraderLiveAdapter(
            app_id="app",
            app_secret="secret",
            access_token="token",
            account_id="1",
            env_values=env_values,
        )
    assert "CTRADER_DEMO" in str(excinfo.value), (
        f"the refusal did not name the variable: {excinfo.value}"
    )


def test_an_absent_ctrader_demo_still_defaults_to_demo(monkeypatch) -> None:
    """The default must survive: only a *present* empty value is an error.

    Distinguishing "blanked" from "never set" is the entire point, so the fix has to
    refuse the first without breaking the second.
    """
    monkeypatch.delenv("CTRADER_DEMO", raising=False)
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="1",
        env_values={},
    )
    assert adapter.demo is True


def test_an_explicit_demo_argument_still_wins_over_the_environment(monkeypatch) -> None:
    """A caller that passed `demo=` has already decided, blanks included.

    Guarding the fallback must not start second-guessing an explicit argument.
    """
    monkeypatch.delenv("CTRADER_DEMO", raising=False)
    adapter = CTraderLiveAdapter(
        app_id="app",
        app_secret="secret",
        access_token="token",
        account_id="1",
        demo=False,
        env_values={"CTRADER_DEMO": ""},
    )
    assert adapter.demo is False
