"""Tests for the MCP server: JSON-RPC handling, both protocol eras, tools and path security.

The protocol handler is exercised through in-memory text streams, exactly as a client
would drive it over stdio; a final group runs the real process through a pipe.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from datamining_skill import (
    InvalidConfigurationException,
    __version__,
    create_mcp_server,
    run_mining,
)
from datamining_skill.application.orchestrator import MiningProgress, MiningSummary
from datamining_skill.infrastructure.mcp_server import McpServer, serve_stdio
from tests.conftest import SCRATCH_ROOT, WriteFile
from tests.support import load_simulation
from tests.test_orchestration import SCALED

sim = load_simulation()
LEGACY_INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}
MESSAGE = dict[str, Any]


def scaled_mine(
    source: Path,
    output: Path,
    *,
    workspace: Path,
    pattern: str | None,
    fields: Sequence[str] | None,
    overwrite: bool,
    csv_formula_guard: bool,
    on_progress: Callable[[MiningProgress], None] | None,
) -> MiningSummary:
    """The real mining runner with small simulated RAM so an 8 MiB file yields ~34 chunks."""
    return run_mining(
        source,
        output,
        workspace=workspace,
        pattern=pattern,
        fields=fields,
        overwrite=overwrite,
        csv_formula_guard=csv_formula_guard,
        on_progress=on_progress,
        chunking_config=SCALED,
        memory_provider=sim.FixedMemory(source.stat().st_size // 5),
    )


class Client:
    """Drives a server over StringIO streams and parses everything it writes."""

    def __init__(self, server: McpServer) -> None:
        self.server = server

    def exchange(self, *messages: MESSAGE | str) -> list[MESSAGE]:
        lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
        stdin = io.StringIO("\n".join(lines) + "\n")
        stdout = io.StringIO()
        self.server.serve(stdin, stdout)
        raw = stdout.getvalue()
        assert "\r" not in raw
        return [json.loads(line) for line in raw.splitlines()]

    def handshake_and(self, *messages: MESSAGE | str) -> list[MESSAGE]:
        """Initialize, then send ``messages``; returns responses after the init reply."""
        return self.exchange(LEGACY_INIT, INITIALIZED, *messages)[1:]


def request(request_id: int | str, method: str, params: MESSAGE | None = None) -> MESSAGE:
    message: MESSAGE = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    return message


def call(request_id: int, name: str, arguments: MESSAGE, **extra: Any) -> MESSAGE:
    return request(request_id, "tools/call", {"name": name, "arguments": arguments, **extra})


@pytest.fixture(scope="module")
def dataset(workspace: Path) -> Iterator[tuple[Path, list[str]]]:
    directory = workspace / f"mcp-data-{uuid.uuid4().hex[:8]}"
    directory.mkdir()
    path = directory / "source.csv"
    expected = cast(list[str], sim.build_email_dataset(path, 8))
    yield path, expected
    shutil.rmtree(directory)


@pytest.fixture
def make_server(state_dir: Path, dataset: tuple[Path, list[str]]) -> Callable[..., McpServer]:
    def _make(**kwargs: Any) -> McpServer:
        kwargs.setdefault("mine_runner", scaled_mine)
        return create_mcp_server([state_dir, dataset[0].parent], **kwargs)

    return _make


@pytest.fixture
def client(make_server: Callable[..., McpServer]) -> Client:
    return Client(make_server())


def tool_text(response: MESSAGE) -> str:
    content = response["result"]["content"]
    assert content[0]["type"] == "text"
    return cast(str, content[0]["text"])




def test_legacy_handshake_negotiates_and_describes_the_server(client: Client) -> None:
    responses = client.exchange(LEGACY_INIT, INITIALIZED)

    assert len(responses) == 1  # the initialized notification gets no reply
    result = responses[0]["result"]
    assert responses[0]["id"] == 1
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"] == {"name": "datamining-skill", "title": "DataMining Skill", "version": __version__}
    assert "profile_dataset" in result["instructions"]


@pytest.mark.parametrize(
    ("requested", "negotiated"),
    [
        ("2024-11-05", "2024-11-05"),
        ("2025-03-26", "2025-03-26"),
        ("2025-06-18", "2025-06-18"),
        ("2025-11-25", "2025-11-25"),
        ("1999-01-01", "2025-11-25"),  # unknown: propose our newest legacy revision
        ("2026-07-28", "2025-11-25"),  # modern revisions do not use `initialize`
    ],
)
def test_version_negotiation(client: Client, requested: str, negotiated: str) -> None:
    init = request(1, "initialize", {"protocolVersion": requested, "capabilities": {}})

    (response,) = client.exchange(init)

    assert response["result"]["protocolVersion"] == negotiated


def test_initialize_requires_a_string_version(client: Client) -> None:
    (response,) = client.exchange(request(1, "initialize", {"capabilities": {}}))

    assert response["error"]["code"] == -32602


def test_ping_works_before_and_after_initialize(client: Client) -> None:
    before, _, after = client.exchange(request(1, "ping"), LEGACY_INIT | {"id": 2}, request(3, "ping"))

    assert before["result"] == {} and after["result"] == {}


def test_tool_requests_before_initialize_are_rejected(client: Client) -> None:
    (response,) = client.exchange(request(5, "tools/list"))

    assert response["id"] == 5
    assert response["error"]["code"] == -32602
    assert "initialize" in response["error"]["message"]




def test_tools_list_declares_both_tools_with_valid_schemas(client: Client) -> None:
    (response,) = client.handshake_and(request(2, "tools/list"))

    result = response["result"]
    assert "resultType" not in result and "ttlMs" not in result  # modern-only fields
    tools = {tool["name"]: tool for tool in result["tools"]}
    assert list(tools) == ["profile_dataset", "mine_dataset"]  # deterministic order
    for tool in tools.values():
        schema = tool["inputSchema"]
        assert schema["type"] == "object" and schema["additionalProperties"] is False
        assert tool["description"] and tool["title"]
        assert set(schema["required"]) <= set(schema["properties"])
        assert tool["annotations"]["openWorldHint"] is False
    assert tools["profile_dataset"]["inputSchema"]["required"] == ["path"]
    assert tools["profile_dataset"]["annotations"]["readOnlyHint"] is True
    assert tools["mine_dataset"]["inputSchema"]["required"] == ["path", "output_path"]
    assert tools["mine_dataset"]["annotations"]["readOnlyHint"] is False


def test_custom_pattern_arguments_are_only_declared_when_enabled(
    make_server: Callable[..., McpServer],
) -> None:
    def mine_properties(server: McpServer) -> set[str]:
        (response,) = Client(server).handshake_and(request(2, "tools/list"))
        mine = next(t for t in response["result"]["tools"] if t["name"] == "mine_dataset")
        return set(mine["inputSchema"]["properties"])

    assert mine_properties(make_server()) == {"path", "output_path", "overwrite", "csv_formula_guard"}
    assert {"pattern", "fields"} <= mine_properties(make_server(allow_custom_patterns=True))




def test_modern_requests_need_no_handshake_and_carry_result_type(client: Client) -> None:
    (listing,) = client.exchange(request(1, "tools/list", {"_meta": MODERN_META}))

    result = listing["result"]
    assert result["resultType"] == "complete"
    assert result["ttlMs"] > 0 and result["cacheScope"] in ("public", "private")
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "datamining-skill"
    assert [t["name"] for t in result["tools"]] == ["profile_dataset", "mine_dataset"]


def test_server_discover_reports_versions_and_capabilities(client: Client) -> None:
    (response,) = client.exchange(request(1, "server/discover", {"_meta": MODERN_META}))

    result = response["result"]
    assert result["resultType"] == "complete"
    assert result["supportedVersions"][0] == "2026-07-28"
    assert "2025-06-18" in result["supportedVersions"]
    assert result["capabilities"] == {"tools": {}}
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["version"] == __version__


def test_unsupported_modern_version_lists_supported_ones(client: Client) -> None:
    meta = MODERN_META | {"io.modelcontextprotocol/protocolVersion": "2999-01-01"}

    (response,) = client.exchange(request(1, "tools/list", {"_meta": meta}))

    error = response["error"]
    assert error["code"] == -32022
    assert error["data"]["requested"] == "2999-01-01"
    assert "2026-07-28" in error["data"]["supported"] and "2025-11-25" in error["data"]["supported"]


@pytest.mark.parametrize(
    "meta",
    [
        {"io.modelcontextprotocol/protocolVersion": "2026-07-28"},  # capabilities missing
        {"io.modelcontextprotocol/protocolVersion": 20260728, "io.modelcontextprotocol/clientCapabilities": {}},
        {"io.modelcontextprotocol/clientCapabilities": {}},  # version missing
    ],
)
def test_modern_request_without_metadata_is_invalid_params(
    client: Client, meta: MESSAGE
) -> None:
    (response,) = client.exchange(request(1, "tools/list", {"_meta": meta}))

    assert response["error"]["code"] == -32602


def test_server_discover_without_metadata_is_invalid_params(client: Client) -> None:
    (response,) = client.exchange(request(1, "server/discover"))

    assert response["error"]["code"] == -32602


def test_ping_does_not_exist_in_the_modern_revision(client: Client) -> None:
    (response,) = client.exchange(request(1, "ping", {"_meta": MODERN_META}))

    assert response["error"]["code"] == -32601


def test_modern_tool_call_has_result_type_and_structure(
    client: Client, dataset: tuple[Path, list[str]]
) -> None:
    params = {"name": "profile_dataset", "arguments": {"path": str(dataset[0])}, "_meta": MODERN_META}

    (response,) = client.exchange(request(1, "tools/call", params))

    result = response["result"]
    assert result["resultType"] == "complete" and result["isError"] is False
    assert result["structuredContent"]["format"] == "csv"
    assert json.loads(tool_text(response)) == result["structuredContent"]




def test_profile_dataset_returns_structure_and_estimate(
    client: Client, dataset: tuple[Path, list[str]]
) -> None:
    (response,) = client.handshake_and(call(2, "profile_dataset", {"path": str(dataset[0])}))

    result = response["result"]
    profile = result["structuredContent"]
    assert result["isError"] is False
    assert json.loads(tool_text(response)) == profile
    assert profile["file"]["name"] == "source.csv"
    assert profile["format"] == "csv"
    assert profile["structure"]["fields"] == ["id", "timestamp", "source", "message"]
    assert profile["records"]["count"] > 50_000
    assert "resultType" not in result  # legacy era


def test_structured_content_is_omitted_for_pre_2025_06_18_sessions(
    make_server: Callable[..., McpServer], dataset: tuple[Path, list[str]]
) -> None:
    init = request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}})

    responses = Client(make_server()).exchange(init, call(2, "profile_dataset", {"path": str(dataset[0])}))

    result = responses[1]["result"]
    assert "structuredContent" not in result
    assert json.loads(tool_text(responses[1]))["format"] == "csv"


def test_relative_paths_resolve_against_workspace(
    client: Client, state_dir: Path
) -> None:
    (state_dir / "sub dir").mkdir()
    (state_dir / "sub dir" / "data file.csv").write_text("a,b\n1,2\n3,4\n")

    forward, native = client.handshake_and(
        call(2, "profile_dataset", {"path": "sub dir/data file.csv"}),
        call(3, "profile_dataset", {"path": os.path.join("sub dir", "data file.csv")}),
    )

    assert forward["result"]["isError"] is False and native["result"]["isError"] is False
    assert forward["result"]["structuredContent"]["records"]["count"] == 2




def read_results(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "email"
    return lines[1:]


def test_mine_dataset_streams_progress_then_returns_the_summary(
    client: Client, dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    source, expected = dataset
    output = state_dir / "out" / "emails.csv"

    messages = client.handshake_and(
        call(2, "mine_dataset", {"path": str(source), "output_path": str(output)}, _meta={"progressToken": "job-1"})
    )

    *progress, final = messages
    assert final["id"] == 2 and "result" in final
    assert progress, "no progress notifications were streamed"
    assert all(m["method"] == "notifications/progress" and "id" not in m for m in progress)
    values = [m["params"]["progress"] for m in progress]
    assert all(m["params"]["progressToken"] == "job-1" for m in progress)
    assert values == sorted(set(values)) and values[0] == 0  # strictly increasing from zero
    totals = {m["params"]["total"] for m in progress}
    assert len(totals) == 1 and 30 <= totals.pop() <= 40
    assert values[-1] == progress[-1]["params"]["total"]  # ends at "all chunks done"
    assert "chunks" in progress[-1]["params"]["message"]

    summary = final["result"]["structuredContent"]
    assert final["result"]["isError"] is False
    assert summary["succeeded"] is True and summary["resumed"] is False
    assert summary["records_written"] == len(expected)
    assert summary["output_path"] == "out/emails.csv"  # workspace-relative, POSIX style
    assert read_results(output) == expected


def test_no_progress_notifications_without_a_progress_token(
    client: Client, dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    messages = client.handshake_and(
        call(2, "mine_dataset", {"path": str(dataset[0]), "output_path": str(state_dir / "o.csv")})
    )

    assert len(messages) == 1 and messages[0]["result"]["isError"] is False


def test_repeat_mining_resumes_and_overwrite_restarts(
    client: Client, dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    source, expected = dataset
    arguments = {"path": str(source), "output_path": str(state_dir / "o.csv")}

    first, second, third = client.handshake_and(
        call(2, "mine_dataset", arguments),
        call(3, "mine_dataset", arguments),
        call(4, "mine_dataset", arguments | {"overwrite": True}),
    )

    assert first["result"]["structuredContent"]["resumed"] is False
    again = second["result"]["structuredContent"]
    assert again["resumed"] is True and again["chunks_processed"] == 0  # nothing left to do
    fresh = third["result"]["structuredContent"]
    assert fresh["resumed"] is False and fresh["chunks_processed"] == fresh["chunks_total"]
    assert read_results(state_dir / "o.csv") == expected  # never duplicated


def test_modern_mine_call_streams_progress_too(
    client: Client, dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    params = {
        "name": "mine_dataset",
        "arguments": {"path": str(dataset[0]), "output_path": str(state_dir / "o.csv")},
        "_meta": MODERN_META | {"progressToken": 7},
    }

    *progress, final = client.exchange(request(1, "tools/call", params))

    assert progress and all(m["params"]["progressToken"] == 7 for m in progress)
    assert final["result"]["resultType"] == "complete"
    assert final["result"]["structuredContent"]["succeeded"] is True


def test_jsonl_output_via_the_extension(
    client: Client, dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    (response,) = client.handshake_and(
        call(2, "mine_dataset", {"path": str(dataset[0]), "output_path": str(state_dir / "o.jsonl")})
    )

    assert response["result"]["isError"] is False
    rows = [json.loads(line) for line in (state_dir / "o.jsonl").read_text("utf-8").splitlines()]
    assert [row["email"] for row in rows] == dataset[1]


def test_custom_pattern_and_fields_when_enabled(
    make_server: Callable[..., McpServer], state_dir: Path
) -> None:
    source = state_dir / "kv.csv"
    source.write_text("id,note\n" + "".join(f"{i},k{i}=v{i};n={i * 2}\n" for i in range(5000)))
    pattern = r"(\w+)=(\w+)"
    server = make_server(allow_custom_patterns=True, mine_runner=None)  # real RAM: tiny file, one chunk

    (response,) = Client(server).handshake_and(
        call(2, "mine_dataset", {
            "path": str(source), "output_path": str(state_dir / "kv.out.csv"),
            "pattern": pattern, "fields": ["key", "value"],
        })
    )

    assert response["result"]["isError"] is False
    lines = (state_dir / "kv.out.csv").read_text().splitlines()
    assert lines[:3] == ["key,value", "k0,v0", "n,0"]
    assert len(lines) == 1 + 5000 * 2


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        ({"pattern": "("}, "invalid regular expression"),
        ({"pattern": "(a)(b)", "fields": ["only_one"]}, "field name"),
        ({"fields": ["a"]}, "requires 'pattern'"),
        ({"pattern": "a", "fields": ["bad;name"]}, "simple names"),
        ({"pattern": "a" * 513}, "longer than 512"),
    ],
)
def test_bad_custom_patterns_are_reported_to_the_model(
    make_server: Callable[..., McpServer],
    dataset: tuple[Path, list[str]],
    state_dir: Path,
    arguments: MESSAGE,
    message: str,
) -> None:
    base = {"path": str(dataset[0]), "output_path": str(state_dir / "o.csv")}

    (response,) = Client(make_server(allow_custom_patterns=True)).handshake_and(
        call(2, "mine_dataset", base | arguments)
    )

    assert response["result"]["isError"] is True
    assert message in tool_text(response)


def test_custom_patterns_are_refused_unless_the_server_opted_in(
    client: Client, dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    (response,) = client.handshake_and(
        call(2, "mine_dataset", {"path": str(dataset[0]), "output_path": str(state_dir / "o.csv"), "pattern": "(a+)+$"})
    )

    assert response["result"]["isError"] is True
    assert "disabled" in tool_text(response)
    assert not (state_dir / "o.csv").exists()




@pytest.mark.parametrize(
    ("tool", "arguments", "message"),
    [
        ("profile_dataset", {}, "missing required argument 'path'"),
        ("profile_dataset", {"path": 5}, "'path' must be a string"),
        ("profile_dataset", {"path": "x", "extra": 1}, "unknown argument 'extra'"),
        ("mine_dataset", {"path": "x"}, "missing required argument 'output_path'"),
        ("mine_dataset", {"path": "x", "output_path": "y.csv", "overwrite": "yes"}, "true or false"),
        ("mine_dataset", {"path": "x" * 5000, "output_path": "y.csv"}, "longer than 4096"),
    ],
)
def test_invalid_arguments_become_correctable_tool_errors(
    client: Client, tool: str, arguments: MESSAGE, message: str
) -> None:
    (response,) = client.handshake_and(call(2, tool, arguments))

    assert response["result"]["isError"] is True
    assert message in tool_text(response)


def test_unknown_tool_and_bad_call_params_are_protocol_errors(client: Client) -> None:
    unknown, no_name, bad_args = client.handshake_and(
        call(2, "delete_everything", {}),
        request(3, "tools/call", {"arguments": {}}),
        request(4, "tools/call", {"name": "profile_dataset", "arguments": ["x"]}),
    )

    assert unknown["error"] == {"code": -32602, "message": "Unknown tool: delete_everything"}
    assert no_name["error"]["code"] == -32602
    assert bad_args["error"]["code"] == -32602




def test_files_outside_the_allowed_directories_are_never_read(
    client: Client, write_file: WriteFile, state_dir: Path
) -> None:
    batch_marker = "payroll-batch-4242"
    outside = Path(tempfile.gettempdir()) / f"mcp-outside-{uuid.uuid4().hex}.csv"
    outside.write_text(f"id,v\n1,{batch_marker}@internal.corp.test\n")
    try:
        responses = client.handshake_and(
            call(2, "profile_dataset", {"path": str(outside)}),
            call(3, "mine_dataset", {"path": str(outside), "output_path": str(state_dir / "o.csv")}),
        )
    finally:
        outside.unlink(missing_ok=True)

    for response in responses:
        assert response["result"]["isError"] is True
        assert "must be located inside" in tool_text(response)
    raw = json.dumps(responses)
    assert batch_marker not in raw and tempfile.gettempdir().replace("\\", "\\\\") not in raw
    assert not (state_dir / "o.csv").exists()


@pytest.mark.parametrize(
    "traversal",
    [
        "../" * 12 + "etc/passwd",
        "..\\" * 12 + "Windows\\win.ini",
        "sub/../../../../outside.csv",
        "/etc/passwd",
        "C:\\Windows\\win.ini",
    ],
)
def test_traversal_attempts_are_rejected(client: Client, traversal: str) -> None:
    (response,) = client.handshake_and(call(2, "profile_dataset", {"path": traversal}))

    assert response["result"]["isError"] is True
    message = tool_text(response)
    assert "must be located inside" in message or "does not name an existing" in message


def test_output_paths_are_confined_too(
    client: Client, dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    outside = Path(tempfile.gettempdir()) / f"mcp-out-{uuid.uuid4().hex}.csv"

    responses = client.handshake_and(
        call(2, "mine_dataset", {"path": str(dataset[0]), "output_path": str(outside)}),
        call(3, "mine_dataset", {"path": str(dataset[0]), "output_path": "../../escape.csv"}),
    )

    assert all(r["result"]["isError"] is True for r in responses)
    assert not outside.exists()
    assert not (state_dir.parent.parent / "escape.csv").exists()


def test_symlink_escaping_the_workspace_is_rejected(client: Client, state_dir: Path) -> None:
    outside = Path(tempfile.gettempdir()) / f"mcp-target-{uuid.uuid4().hex}.csv"
    outside.write_text("a,b\n1,2\n")
    link = state_dir / "innocent.csv"
    try:
        try:
            os.symlink(outside, link)
        except (OSError, NotImplementedError):
            pytest.skip("symbolic links are not available to this user")
        (response,) = client.handshake_and(call(2, "profile_dataset", {"path": "innocent.csv"}))
    finally:
        link.unlink(missing_ok=True)
        outside.unlink(missing_ok=True)

    assert response["result"]["isError"] is True
    assert "must be located inside" in tool_text(response)


@pytest.mark.parametrize("bad", ["", "   ", "has\0nul", "x" * 5000])
def test_malformed_path_values_are_rejected(client: Client, bad: str) -> None:
    (response,) = client.handshake_and(call(2, "profile_dataset", {"path": bad}))

    assert response["result"]["isError"] is True


def test_directories_and_missing_files_are_not_profiled(client: Client, state_dir: Path) -> None:
    directory, missing = client.handshake_and(
        call(2, "profile_dataset", {"path": str(state_dir)}),
        call(3, "profile_dataset", {"path": "nope.csv"}),
    )

    assert directory["result"]["isError"] is True
    assert "existing regular file" in tool_text(missing)


@pytest.mark.skipif(os.name != "nt", reason="NTFS alternate data streams")
def test_alternate_data_streams_are_rejected(client: Client, state_dir: Path) -> None:
    (state_dir / "host.csv").write_text("a,b\n1,2\n")

    (response,) = client.handshake_and(call(2, "profile_dataset", {"path": "host.csv:hidden"}))

    assert response["result"]["isError"] is True
    assert "stream" in tool_text(response)


def test_output_safety_rules(
    client: Client, dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    copy = state_dir / "copy.csv"
    shutil.copy(dataset[0], copy)
    existing = state_dir / "keep.csv"
    existing.write_text("retained export\n")

    wrong_ext, same_file, clobber = client.handshake_and(
        call(2, "mine_dataset", {"path": str(dataset[0]), "output_path": str(state_dir / "o.txt")}),
        call(3, "mine_dataset", {"path": str(copy), "output_path": str(copy)}),
        call(4, "mine_dataset", {"path": str(dataset[0]), "output_path": str(existing)}),
    )

    assert ".csv, .jsonl" in tool_text(wrong_ext)
    assert "must not be the source" in tool_text(same_file)
    assert "already exists" in tool_text(clobber)
    assert existing.read_text() == "retained export\n" and copy.read_bytes() == dataset[0].read_bytes()


def test_allowed_directories_must_exist(state_dir: Path) -> None:
    with pytest.raises(InvalidConfigurationException, match="does not exist"):
        create_mcp_server([state_dir / "missing"])
    with pytest.raises(InvalidConfigurationException, match="at least one"):
        create_mcp_server([])




def test_parse_errors_use_a_null_id_and_do_not_stop_the_server(client: Client) -> None:
    responses = client.exchange("{not json", request(2, "ping"))

    assert responses[0] == {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
    assert responses[1]["result"] == {}


@pytest.mark.parametrize(
    ("raw", "expected_id"),
    [
        ('"just a string"', None),
        ("42", None),
        ("null", None),
        ('[{"jsonrpc":"2.0","id":1,"method":"ping"}]', None),  # batches unsupported
        ("[]", None),
        ('{"jsonrpc":"2.0","id":9}', 9),  # no method
        ('{"jsonrpc":"1.0","id":3,"method":"ping"}', 3),  # wrong version
        ('{"id":4,"method":"ping"}', 4),  # no version
        ('{"jsonrpc":"2.0","id":null,"method":"ping"}', None),  # null id forbidden by MCP
        ('{"jsonrpc":"2.0","id":true,"method":"ping"}', None),
        ('{"jsonrpc":"2.0","id":1.5,"method":"ping"}', None),
        ('{"jsonrpc":"2.0","id":5,"method":7}', 5),
    ],
)
def test_invalid_requests(client: Client, raw: str, expected_id: int | None) -> None:
    (response,) = client.exchange(raw)

    assert response["error"]["code"] == -32600
    assert response["id"] == expected_id


def test_params_must_be_an_object(client: Client) -> None:
    (response,) = client.exchange('{"jsonrpc":"2.0","id":8,"method":"ping","params":[1]}')

    assert response["error"]["code"] == -32602 and response["id"] == 8


def test_unknown_methods_error_for_requests_but_not_notifications(client: Client) -> None:
    responses = client.exchange(
        request("abc", "resources/list"),
        {"jsonrpc": "2.0", "method": "notifications/whatever"},
        request(3, "ping"),
    )

    assert responses[0]["id"] == "abc" and responses[0]["error"]["code"] == -32601
    assert responses[1]["id"] == 3  # the unknown notification produced no output


def test_blank_lines_and_stray_responses_are_ignored(client: Client) -> None:
    responses = client.exchange("", "   ", '{"jsonrpc":"2.0","id":1,"result":{}}', request(2, "ping"))

    assert [r["id"] for r in responses] == [2]


def test_oversized_messages_are_rejected_without_losing_sync(
    make_server: Callable[..., McpServer],
) -> None:
    server = make_server(max_message_chars=1000)
    big = '{"jsonrpc":"2.0","id":1,"method":"ping","params":{"pad":"' + "x" * 5000 + '"}}'

    responses = Client(server).exchange(big, request(2, "ping"))

    assert responses[0]["error"]["code"] == -32600 and "too large" in responses[0]["error"]["message"]
    assert responses[1]["id"] == 2 and responses[1]["result"] == {}


def test_handler_bugs_become_internal_errors_without_details(
    make_server: Callable[..., McpServer], monkeypatch: pytest.MonkeyPatch
) -> None:
    server = make_server()
    client = Client(server)
    monkeypatch.setattr(server._tools, "definitions", lambda: 1 / 0)  # noqa: SLF001

    broken, healthy = client.handshake_and(request(2, "tools/list"), request(3, "ping"))

    assert broken["error"] == {"code": -32603, "message": "Internal error"}
    assert healthy["result"] == {}


def test_crashing_tool_becomes_tool_error_server_survives(
    make_server: Callable[..., McpServer], dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    def exploding(*_args: Any, **_kwargs: Any) -> MiningSummary:
        raise RuntimeError("boom with /srv/hr/exports")

    server = make_server(mine_runner=exploding)

    crashed, alive = Client(server).handshake_and(
        call(2, "mine_dataset", {"path": str(dataset[0]), "output_path": str(state_dir / "o.csv")}),
        request(3, "ping"),
    )

    assert crashed["result"]["isError"] is True
    assert "RuntimeError" in tool_text(crashed) and "/srv/hr/exports" not in tool_text(crashed)
    assert alive["result"] == {}


def test_stdout_carries_only_ascii_json_rpc_messages(
    client: Client, state_dir: Path
) -> None:
    source = state_dir / "ünïcode dir"
    source.mkdir()
    (source / "dätä.csv").write_text("naïve,b\n1,2\n", encoding="utf-8")
    stdout = io.StringIO()
    stdin = io.StringIO(
        "\n".join(
            json.dumps(m)
            for m in (LEGACY_INIT, INITIALIZED, call(2, "profile_dataset", {"path": "ünïcode dir/dätä.csv"}))
        )
        + "\n"
    )

    client.server.serve(stdin, stdout)

    raw = stdout.getvalue()
    assert raw.isascii()  # safe on any console code page
    lines = raw.splitlines()
    assert all(json.loads(line)["jsonrpc"] == "2.0" for line in lines)
    assert "naïve" in json.dumps(json.loads(lines[1]), ensure_ascii=False)  # survives the round trip
    assert raw.count("\n") == len(lines)  # exactly one message per line




def run_server_process(
    allowed: Path, messages: Sequence[MESSAGE | str], *extra_args: str
) -> subprocess.CompletedProcess[str]:
    stdin = "\n".join(m if isinstance(m, str) else json.dumps(m) for m in messages) + "\n"
    return subprocess.run(
        [sys.executable, "-m", "datamining_skill.cli", "mcp", "--allow-dir", str(allowed), *extra_args],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=120,
    )


def test_cli_mcp_runs_a_full_session_over_real_pipes(state_dir: Path) -> None:
    source = state_dir / "contacts.csv"
    source.write_text(
        "id,note\n" + "".join(f"{i},mail me at emp{i}@internal.corp.test please\n" for i in range(2000)),
        encoding="utf-8",
    )

    result = run_server_process(
        state_dir,
        [
            LEGACY_INIT,
            INITIALIZED,
            request(2, "tools/list"),
            call(3, "profile_dataset", {"path": "contacts.csv"}),
            call(4, "mine_dataset", {"path": "contacts.csv", "output_path": "found.csv"}, _meta={"progressToken": "p"}),
        ],
    )

    assert result.returncode == 0, result.stderr  # exits cleanly when stdin closes
    messages = [json.loads(line) for line in result.stdout.splitlines()]  # every line is JSON
    assert all(m["jsonrpc"] == "2.0" for m in messages)
    by_id = {m["id"]: m for m in messages if "id" in m}
    assert by_id[1]["result"]["serverInfo"]["name"] == "datamining-skill"
    assert len(by_id[2]["result"]["tools"]) == 2
    assert by_id[3]["result"]["structuredContent"]["records"]["count"] == 2000
    assert by_id[4]["result"]["structuredContent"]["records_written"] == 2000
    assert any(m.get("method") == "notifications/progress" for m in messages)
    found = (state_dir / "found.csv").read_text().splitlines()
    assert found[0] == "email" and found[1] == "emp0@internal.corp.test" and len(found) == 2001


def test_cli_mcp_logs_go_to_stderr_never_stdout(state_dir: Path) -> None:
    result = run_server_process(state_dir, [LEGACY_INIT], "--log-level", "DEBUG")

    assert result.returncode == 0
    assert len(result.stdout.splitlines()) == 1  # only the initialize response
    json.loads(result.stdout)


def test_cli_mcp_refuses_a_missing_allowed_directory(state_dir: Path) -> None:
    result = run_server_process(state_dir / "missing", [LEGACY_INIT])

    assert result.returncode == 3
    assert result.stdout == "" and "does not exist" in result.stderr


def test_cli_mcp_help_documents_the_options() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "datamining_skill.cli", "mcp", "--help"],
        capture_output=True, text=True, check=False, timeout=60,
    )

    assert result.returncode == 0
    assert "--allow-dir" in result.stdout and "--allow-custom-patterns" in result.stdout




def test_pathologically_deep_json_is_a_parse_error_not_a_crash(client: Client) -> None:
    deep = "[" * 200_000 + "]" * 200_000

    responses = client.exchange(deep, request(2, "ping"))

    assert responses[0]["error"]["code"] == -32700
    assert responses[1]["result"] == {}  # still serving


def test_a_stray_print_cannot_corrupt_the_protocol_stream(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (state_dir / "t.csv").write_text("a,b\n1,2\n")

    def chatty_profile(_path: Path) -> dict[str, Any]:
        print("STRAY OUTPUT FROM SOME LIBRARY")
        return {"ok": True}

    server = create_mcp_server([state_dir], profile_runner=chatty_profile)
    messages = [LEGACY_INIT, INITIALIZED, call(2, "profile_dataset", {"path": "t.csv"})]
    fake_stdin = io.StringIO("\n".join(json.dumps(m) for m in messages) + "\n")
    fake_stdout, fake_stderr = io.StringIO(), io.StringIO()
    monkeypatch.setattr(sys, "stdin", fake_stdin)
    monkeypatch.setattr(sys, "stdout", fake_stdout)
    monkeypatch.setattr(sys, "stderr", fake_stderr)

    serve_stdio(server)

    assert [json.loads(line)["id"] for line in fake_stdout.getvalue().splitlines()] == [1, 2]
    assert "STRAY OUTPUT" not in fake_stdout.getvalue()
    assert "STRAY OUTPUT" in fake_stderr.getvalue()
    assert sys.stdout is fake_stdout  # restored after serving


def test_file_system_error_is_reported_readably(
    make_server: Callable[..., McpServer], dataset: tuple[Path, list[str]], state_dir: Path
) -> None:
    def denied(*_args: Any, **_kwargs: Any) -> MiningSummary:
        raise PermissionError(13, "Permission denied", str(state_dir / "restricted" / "found.csv"))

    server = make_server(mine_runner=denied)

    (response,) = Client(server).handshake_and(
        call(2, "mine_dataset", {"path": str(dataset[0]), "output_path": str(state_dir / "o.csv")})
    )

    text = response["result"]["content"][0]["text"]
    assert response["result"]["isError"] is True
    assert text == "file system error: Permission denied"  # no path, no 'internal error'
