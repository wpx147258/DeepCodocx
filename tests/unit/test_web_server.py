from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from cli_agent.assembly import RuntimeBundle
from cli_agent.config import AppPaths, load_config
from cli_agent.messages import user_message
from cli_agent.permissions.policy import PermissionRequest
from cli_agent.providers.base import ModelProfile
from cli_agent.session.store import SessionStore
from cli_agent.web import server as web_server
from cli_agent.web.server import (
    STATIC_DIR,
    WebAsker,
    WebHost,
    _model_choices,
    create_app,
    session_summaries,
)
from tests.support.fake_provider import FakeProvider, text_response, tool_response
from tests.support.runtime_fixture import EchoTool, make_runtime


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
    }


def _bundle(
    tmp_path: Path,
    provider: FakeProvider,
    *,
    tools=None,
    session_id: str = "test-session",
) -> RuntimeBundle:
    runtime, _ = make_runtime(provider, tmp_path=tmp_path, tools=tools)
    config = load_config(cwd=tmp_path, env=_env(tmp_path))
    return RuntimeBundle(
        runtime=runtime,
        config=config,
        profile=runtime._deps.profile,
        session_id=session_id,
    )


def _events(body: str) -> list[dict]:
    events = []
    for line in body.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


@pytest.fixture
async def client_factory(tmp_path: Path):
    created: list[RuntimeBundle] = []

    async def build(
        scripts,
        *,
        tools=None,
        profile: ModelProfile | None = None,
        policy=None,
    ):
        provider = FakeProvider(scripts)
        runtime, _ = make_runtime(
            provider,
            tmp_path=tmp_path,
            tools=tools,
            profile=profile,
            policy=policy,
        )
        config = load_config(cwd=tmp_path, env=_env(tmp_path))
        bundle = RuntimeBundle(
            runtime=runtime,
            config=config,
            profile=runtime._deps.profile,
            session_id="test-session",
        )
        created.append(bundle)
        app = create_app(bundle)
        transport = httpx.ASGITransport(app=app)
        client = httpx.AsyncClient(transport=transport, base_url="http://test")
        return client, bundle, app

    yield build
    for bundle in created:
        await bundle.runtime.close()


async def test_bootstrap_and_streamed_turn(client_factory) -> None:
    client, bundle, _ = await client_factory([[text_response("hello")]])
    async with client:
        bootstrap = await client.get("/api/bootstrap")
        assert bootstrap.status_code == 200
        data = bootstrap.json()
        assert data["session_id"] == "test-session"
        assert data["messages"] == []
        assert data["model"] == "fake-model"
        assert data["models"]

        response = await client.post("/api/chat", json={"text": "hi"})
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events = _events(response.text)
        kinds = [event["type"] for event in events]
        assert kinds[0] == "turn_start"
        assert "text" in kinds
        assert kinds[-1] == "done"
        assert events[-1]["stop_reason"] == "end_turn"
        assert any(event["type"] == "text" and event["text"] == "hello" for event in events)

        after = (await client.get("/api/bootstrap")).json()
        assert [message["role"] for message in after["messages"]] == [
            "user",
            "assistant",
        ]
        assert after["messages"][-1]["content"][0]["text"] == "hello"
    assert bundle.runtime.messages


async def test_chat_validates_input(client_factory) -> None:
    client, _, _ = await client_factory([])
    async with client:
        empty = await client.post("/api/chat", json={"text": "  "})
        assert empty.status_code == 400
        bad = await client.post(
            "/api/chat",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert bad.status_code == 400


async def test_tool_events_reach_the_browser(client_factory) -> None:
    client, bundle, _ = await client_factory(
        [
            [tool_response([("t1", "Echo", {"text": "ping"})])],
            [text_response("done")],
        ],
        tools=[EchoTool()],
    )
    async with client:
        response = await client.post("/api/chat", json={"text": "go"})
        events = _events(response.text)
    started = [event for event in events if event["type"] == "tool_started"]
    finished = [event for event in events if event["type"] == "tool_finished"]
    # FakeProvider completes the message without separate ToolCall events; the
    # UI must still render the result.
    assert started == []
    assert finished[0]["name"] == "Echo"
    assert finished[0]["content"][0]["text"] == "ping"
    assert not finished[0]["is_error"]


async def test_clear_and_auto_endpoints(client_factory) -> None:
    from cli_agent.permissions.policy import PermissionMode, PermissionPolicy

    client, bundle, _ = await client_factory(
        [[text_response("hello")]],
        policy=PermissionPolicy(PermissionMode.DEFAULT, workspace=Path.cwd()),
    )
    async with client:
        await client.post("/api/chat", json={"text": "hi"})
        cleared = await client.post("/api/clear")
        assert cleared.status_code == 200
        assert cleared.json()["messages"] == []

        auto = await client.post("/api/auto", json={"enabled": True})
        assert auto.json()["permission_mode"] == "bypass"
        manual = await client.post("/api/auto", json={"enabled": False})
        assert manual.json()["permission_mode"] == "default"
    assert bundle.runtime.messages == ()


async def test_model_switch_endpoint(client_factory) -> None:
    client, bundle, _ = await client_factory([[text_response("old")]])
    new_provider = FakeProvider([[text_response("new")]])
    profile = ModelProfile(
        "mock2", "anthropic_compat", "model-2", 1_000, 100
    )
    bundle.runtime._deps.provider_factory = (
        lambda provider, model: (new_provider, profile, None)
    )
    async with client:
        response = await client.post(
            "/api/model", json={"provider": "mock2", "model": "model-2"}
        )
        assert response.status_code == 200
        assert response.json()["provider"] == "mock2"
        assert response.json()["model"] == "model-2"
        bad = await client.post("/api/model", json={"provider": "", "model": ""})
        assert bad.status_code == 400


async def test_permission_bridge_round_trip(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, FakeProvider([]))
    host = WebHost(bundle)
    host._queue = asyncio.Queue()
    request = PermissionRequest(
        tool_name="Bash",
        tool_input={"command": "rm -rf x"},
        reason="shell tool in default mode",
    )
    task = asyncio.create_task(host.request_permission(request))
    event = await asyncio.wait_for(host._queue.get(), timeout=1)
    assert event["type"] == "permission_request"
    assert event["tool"] == "Bash"
    assert host.resolve_permission(event["id"], "always")
    assert await task == "always"
    assert not host.resolve_permission("missing", "yes")
    await bundle.runtime.close()


async def test_permission_endpoint_validates(client_factory) -> None:
    client, _, _ = await client_factory([])
    async with client:
        bad = await client.post(
            "/api/permission", json={"id": "x", "decision": "maybe"}
        )
        assert bad.status_code == 400
        broken = await client.post(
            "/api/permission",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert broken.status_code == 400
        unknown = await client.post(
            "/api/permission", json={"id": "x", "decision": "yes"}
        )
        assert unknown.status_code == 404


async def test_static_assets_and_headers(client_factory) -> None:
    client, _, _ = await client_factory([])
    async with client:
        index = await client.get("/")
        assert index.status_code == 200
        assert "cli-agent" in index.text
        assert "Content-Security-Policy" in index.headers
        assert "default-src 'self'" in index.headers["Content-Security-Policy"]
        script = await client.get("/static/app.js")
        assert script.status_code == 200
        assert "text/javascript" in script.headers["content-type"]
        style = await client.get("/static/styles.css")
        assert style.status_code == 200
        icon = await client.get("/favicon.svg")
        assert icon.status_code == 200


async def test_compact_endpoint(client_factory) -> None:
    client, _, _ = await client_factory([])
    async with client:
        response = await client.post("/api/compact")
        assert response.status_code == 200
        body = response.json()
        assert "events" in body
        assert body["state"]["model"] == "fake-model"


async def test_asker_without_host_denies() -> None:
    asker = WebAsker()
    answer = await asker.ask(
        PermissionRequest("Bash", {"command": "ls"}, "shell tool")
    )
    assert answer == "no"


def test_session_summaries_use_first_user_message(tmp_path: Path) -> None:
    paths = AppPaths.discover(tmp_path, env=_env(tmp_path))
    store = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    store.append_message(user_message("Fix the parser bug"))
    session_id = store.session_id
    store.close()
    empty = SessionStore.new(
        paths, provider="mock", model="mock-1", cwd=tmp_path, persist=True
    )
    empty.close()

    summaries = session_summaries(paths, current=session_id)
    assert [summary["id"] for summary in summaries] == [session_id]
    assert summaries[0]["title"] == "Fix the parser bug"
    assert summaries[0]["current"] is True
    assert summaries[0]["model"] == "mock-1"


async def test_new_session_endpoint_swaps_the_runtime(tmp_path: Path) -> None:
    first_provider = FakeProvider([[text_response("first")]])
    first = _bundle(tmp_path, first_provider)
    second_provider = FakeProvider([[text_response("second")]])
    second = _bundle(tmp_path, second_provider, session_id="second-session")
    calls: list[str | None] = []

    def factory(session_id: str | None) -> RuntimeBundle:
        calls.append(session_id)
        return second

    app = create_app(first, factory=factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        listed = await client.get("/api/sessions")
        assert listed.status_code == 200
        assert listed.json()["sessions"] == []

        response = await client.post("/api/new")
        assert response.status_code == 200
        assert response.json()["session_id"] == "second-session"
        assert calls == [None]
        assert first_provider.closed
        assert app.state.host.runtime is second.runtime
    await second.runtime.close()


async def test_open_session_endpoint_passes_the_id(tmp_path: Path) -> None:
    first = _bundle(tmp_path, FakeProvider([[text_response("first")]]))
    second = _bundle(
        tmp_path,
        FakeProvider([[text_response("second")]]),
        session_id="resumed-session",
    )
    calls: list[str | None] = []

    def factory(session_id: str | None) -> RuntimeBundle:
        calls.append(session_id)
        return second

    app = create_app(first, factory=factory)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/open", json={"id": "sess_123"})
        assert response.status_code == 200
        assert calls == ["sess_123"]
        assert response.json()["session_id"] == "resumed-session"
        missing = await client.post("/api/open", json={})
        assert missing.status_code == 400
    await second.runtime.close()


def test_static_assets_are_self_contained() -> None:
    for name in ("index.html", "app.js", "styles.css", "favicon.svg"):
        assert (STATIC_DIR / name).is_file()
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert '/static/app.js' in html
    assert '/static/styles.css' in html
    assert "http://" not in html
    assert "https://" not in html
    assert "eval(" not in script


def test_model_choices_hide_providers_without_credentials(
    tmp_path: Path, monkeypatch
) -> None:
    bundle = _bundle(tmp_path, FakeProvider([]))
    monkeypatch.setattr(web_server, "resolve_api_key", lambda provider, env: None)
    choices = _model_choices(bundle)
    assert [choice["provider"] for choice in choices] == ["fake"]
    assert choices[0]["model"] == "fake-model"


def test_auto_open_skips_headless_and_wsl(monkeypatch) -> None:
    monkeypatch.delenv("BROWSER", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    monkeypatch.setattr(web_server, "_is_wsl", lambda: False)
    assert web_server._can_auto_open() is False

    monkeypatch.setattr(web_server, "_is_wsl", lambda: True)
    monkeypatch.setattr(web_server.shutil, "which", lambda name: None)
    assert web_server._can_auto_open() is False
    monkeypatch.setattr(
        web_server.shutil, "which",
        lambda name: "/usr/bin/wslview" if name == "wslview" else None,
    )
    assert web_server._can_auto_open() is True

    monkeypatch.setenv("BROWSER", "custom-browser")
    assert web_server._can_auto_open() is True
