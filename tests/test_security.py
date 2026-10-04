"""Security regression suite: path confinement, hostile patterns, isolation, SQL and permissions.

Every attack below must fail *closed*: rejected with a category-only message that echoes
neither the payload nor any absolute path, before any file is read or written.
"""

from __future__ import annotations

import ast
import json
import logging
import os
import re
import socket
import sqlite3
import stat
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from datamining_skill import (
    ChunkMetadata,
    ChunkStatus,
    InvalidConfigurationException,
    StateManager,
    UnsupportedDataFormatException,
    create_mcp_server,
    create_profiler,
    run_mining,
)
from datamining_skill.application.extraction_strategy import EMAIL_PATTERN, vet_pattern
from datamining_skill.cli import CUSTOM_PATTERN_WARNING, main
from datamining_skill.domain.exceptions import printable
from datamining_skill.infrastructure.handlers.log_handler import _MATCH_PREFIX_CHARS, _PATTERNS
from datamining_skill.infrastructure.mcp_tools import ToolInputError, WorkspacePolicy
from datamining_skill.infrastructure.paths import check_path_text
from datamining_skill.infrastructure.permissions import ensure_private_directory, restrict_to_owner
from datamining_skill.infrastructure.scratch import LocalScratchStore
from tests.conftest import SCRATCH_ROOT
from tests.support import describe_dacl, make_directory_link, remove_link
from tests.test_mcp_server import INITIALIZED, LEGACY_INIT, MODERN_META, Client, call, request, tool_text

ON_WINDOWS = os.name == "nt"
windows_only = pytest.mark.skipif(not ON_WINDOWS, reason="Windows path semantics")
posix_only = pytest.mark.skipif(ON_WINDOWS, reason="POSIX permission bits")
SRC = Path(__file__).resolve().parent.parent / "src" / "datamining_skill"
PAYLOAD_MARKER = "tok-9f2c71b8"
MAIL_ROWS = "id,note\n" + "".join(f"{i},escalate to sys.admin{i}@internal.corp.test\n" for i in range(40))


@dataclass
class Env:
    workspace: Path
    outside: Path
    payroll_file: Path
    policy: WorkspacePolicy
    links: list[Path] = field(default_factory=list)

    def link(self, name: str, target: Path) -> Path:
        path = self.workspace / name
        if not make_directory_link(path, target):
            pytest.skip("neither symlinks nor junctions can be created here")
        self.links.append(path)
        return path


@pytest.fixture
def env(state_dir: Path) -> Iterator[Env]:
    workspace, outside = state_dir / "workspace", state_dir / "outside"
    for directory in (workspace / "sub", outside):
        directory.mkdir(parents=True)
    (workspace / "ok.csv").write_text(MAIL_ROWS, encoding="utf-8")
    (workspace / "sub" / "data.csv").write_text(MAIL_ROWS, encoding="utf-8")
    payroll_file = outside / "payroll.csv"
    payroll_file.write_text(f"id,token\n1,{PAYLOAD_MARKER}@internal.corp.test\n", encoding="utf-8")
    environment = Env(workspace, outside, payroll_file, WorkspacePolicy([workspace]))
    yield environment
    for link in environment.links:
        remove_link(link)


def profile_via_mcp(env: Env, raw: Any) -> dict[str, Any]:
    (response,) = Client(create_mcp_server([env.workspace])).handshake_and(
        call(2, "profile_dataset", {"path": raw})
    )
    return response


# ================================================================== path confinement

POSIX_TRAVERSAL = [
    "../outside/payroll.csv",
    "./../outside/payroll.csv",
    "sub/../../outside/payroll.csv",
    "{outside}/payroll.csv",
    "../" * 14 + "etc/passwd",
]
WINDOWS_TRAVERSAL = [
    "..\\outside\\payroll.csv",
    "sub\\..\\..\\outside\\payroll.csv",
    "..\\" * 14 + "Windows\\win.ini",
    "C:\\Windows\\win.ini",
    "\\Windows\\win.ini",
    "C:..\\outside\\payroll.csv",
]
TRAVERSAL = POSIX_TRAVERSAL + (WINDOWS_TRAVERSAL if ON_WINDOWS else [])


@pytest.mark.parametrize("payload", [pytest.param(p, id=f"traversal-{i}") for i, p in enumerate(TRAVERSAL)])
def test_traversal_is_rejected_and_nothing_is_disclosed(env: Env, state_dir: Path, payload: str) -> None:
    raw = payload.replace("{outside}", str(env.outside))

    with pytest.raises(ToolInputError):  # by containment or by a stricter text rule: either way, refused
        env.policy.resolve(raw, "path")

    response = profile_via_mcp(env, raw)
    text = json.dumps(response)
    assert response["result"]["isError"] is True
    for forbidden in (PAYLOAD_MARKER, str(env.outside), str(env.workspace), str(state_dir)):
        assert forbidden.replace("\\", "\\\\") not in text and forbidden not in text


@pytest.mark.parametrize(
    "literal",
    ["%2e%2e/payroll.csv", "..%2fpayroll.csv", "~/ok.csv", "$HOME/ok.csv", "${HOME}/ok.csv", "%USERPROFILE%/ok.csv", "ok%00.csv"],
)
def test_nothing_is_decoded_or_expanded(env: Env, literal: str) -> None:
    """URL escapes, ``~`` and environment references are ordinary file-name characters."""
    resolved = env.policy.resolve(literal, "path")

    assert resolved == (env.workspace.resolve() / Path(literal)).resolve()
    assert resolved.is_relative_to(env.workspace.resolve())


@pytest.mark.parametrize("name", ["a\x00.csv", "a\n.csv", "a\r.csv", "a\t.csv", "a\x1b[31m.csv", "a\x7f.csv"])
def test_control_characters_rejected_before_any_fs_access(
    env: Env, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def touched(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("the filesystem was consulted before the path text was vetted")

    monkeypatch.setattr(Path, "resolve", touched)

    with pytest.raises(ToolInputError, match="control characters"):
        env.policy.resolve(name, "path")


@pytest.mark.parametrize("name", ["ok.csv/..namedfork/rsrc", "OK.CSV/..NAMEDFORK/rsrc"])
def test_macos_resource_forks_are_rejected(env: Env, name: str) -> None:
    with pytest.raises(ToolInputError, match="resource fork"):
        env.policy.resolve(name, "path")


WINDOWS_HAZARDS = [
    # UNC and device paths: resolving these makes Windows open a network connection
    r"\\192.0.2.1\share\x.csv", "//192.0.2.1/share/x.csv", r"\\localhost\c$\Windows\win.ini",
    r"\\?\C:\Windows\win.ini", r"\\?\UNC\192.0.2.1\share\x.csv", r"\\.\NUL", r"\\.\PhysicalDrive0",
    # alternate data streams
    "ok.csv:stream", "ok.csv::$DATA", "ok.csv:$DATA", "sub:ads/x.csv", "ok.csv:Zone.Identifier",
    # reserved device names, with and without extension
    "NUL", "nul", "CON", "sub/CON.txt", "COM1", "LPT1.csv", "aux.csv", "PRN", "CONIN$", "COM\u00b9",
    # names that Windows silently normalises into another name
    "ok.csv.", "ok.csv ", "sub./x.csv", ".. /payroll.csv", "sub /x.csv",
]  # fmt: skip


@windows_only
@pytest.mark.parametrize("payload", [pytest.param(p, id=f"win-{i}") for i, p in enumerate(WINDOWS_HAZARDS)])
def test_windows_path_hazards_rejected_before_any_fs_access(
    env: Env, payload: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def touched(*_args: object, **_kwargs: object) -> Path:
        raise AssertionError("a UNC or device path reached the filesystem layer")

    monkeypatch.setattr(Path, "resolve", touched)

    with pytest.raises(ToolInputError):
        env.policy.resolve(payload, "path")


@windows_only
def test_a_unc_path_is_refused_instantly_instead_of_dialling_out(env: Env) -> None:
    """Before the fix, 192.0.2.1 (an unroutable test address) stalled path validation for ~20 s."""
    started = time.perf_counter()
    with pytest.raises(ToolInputError, match="network or device"):
        env.policy.resolve(r"\\192.0.2.1\share\data.csv", "path")

    assert time.perf_counter() - started < 1.0


@windows_only
@pytest.mark.parametrize(
    "name", ["con_sole.csv", "nul-data.csv", "COM10.csv", "dir.name/file.v2.csv", "file with spaces.csv", "ok..csv", "x/..x/y.csv"]
)
def test_legitimate_windows_names_next_to_the_hazards_still_work(env: Env, name: str) -> None:
    assert env.policy.resolve(name, "path").is_relative_to(env.workspace.resolve())


@pytest.mark.parametrize("raw", ["ok.csv", "sub/data.csv", "./sub/../ok.csv", "sub/deeper/not-yet.csv"])
def test_ordinary_paths_inside_the_workspace_are_accepted(env: Env, raw: str) -> None:
    assert env.policy.resolve(raw, "path").is_relative_to(env.workspace.resolve())


def test_check_path_text_error_messages_never_echo_the_input() -> None:
    hostile = "very-recognisable-\x1b[31m-payload"

    with pytest.raises(InvalidConfigurationException) as caught:
        check_path_text(hostile, "'path'")

    assert "recognisable" not in str(caught.value)


# ============================================================================== links


def test_link_out_of_workspace_cannot_read_or_write(env: Env) -> None:
    env.link("portal", env.outside)

    for raw in ("portal/payroll.csv", "portal"):
        with pytest.raises(ToolInputError, match="allowed directories"):
            env.policy.resolve(raw, "path")
    (read, write) = Client(create_mcp_server([env.workspace])).handshake_and(
        call(2, "profile_dataset", {"path": "portal/payroll.csv"}),
        call(3, "mine_dataset", {"path": "ok.csv", "output_path": "portal/stolen.csv"}),
    )

    assert read["result"]["isError"] and write["result"]["isError"]
    assert PAYLOAD_MARKER not in json.dumps(read)
    assert not (env.outside / "stolen.csv").exists()
    assert sorted(p.name for p in env.outside.iterdir()) == ["payroll.csv"]  # nothing was created there


def test_a_link_that_stays_inside_the_workspace_is_fine(env: Env) -> None:
    env.link("alias", env.workspace / "sub")

    (response,) = Client(create_mcp_server([env.workspace])).handshake_and(
        call(2, "profile_dataset", {"path": "alias/data.csv"})
    )

    assert response["result"]["isError"] is False


def test_link_loops_are_reported_cleanly(env: Env) -> None:
    first, second = env.workspace / "loop-a", env.workspace / "loop-b"
    second.mkdir()
    env.links.extend([first, second])
    if not make_directory_link(first, second):
        pytest.skip("links unavailable")
    second.rmdir()
    if not make_directory_link(second, first):
        pytest.skip("links unavailable")

    read, write = Client(create_mcp_server([env.workspace])).handshake_and(
        call(2, "profile_dataset", {"path": "loop-a/x.csv"}),
        call(3, "mine_dataset", {"path": "ok.csv", "output_path": "loop-a/out.csv"}),
    )

    assert read["result"]["isError"] is True and write["result"]["isError"] is True


def test_scratch_directory_link_out_of_workspace_is_refused(env: Env) -> None:
    """A planted ``.scratch`` link would send state and chunk files (mined data) elsewhere."""
    env.link(".scratch", env.outside)

    with pytest.raises(InvalidConfigurationException, match="leads outside"):
        run_mining(env.workspace / "ok.csv", env.workspace / "out.csv", workspace=env.workspace)
    (response,) = Client(create_mcp_server([env.workspace])).handshake_and(
        call(2, "mine_dataset", {"path": "ok.csv", "output_path": "out.csv"})
    )

    assert response["result"]["isError"] is True and "leads outside" in tool_text(response)
    assert sorted(p.name for p in env.outside.iterdir()) == ["payroll.csv"]
    assert not (env.workspace / "out.csv").exists()


# ================================================================================ ReDoS


def hostile_lines(limit: int) -> Iterator[str]:
    fragments = ['"', "[", "]", " ", "\t", ":", "-", "a", "1", ".", "@", ",", "/", "Mar  1 00:00:00 ",
                 "2025-01-01T00:00:00Z ", "ERROR ", "127.0.0.1 - - [", '" 200 ', "sshd[1]: "]  # fmt: skip
    for fragment in fragments:
        yield (fragment * limit)[:limit]
    yield "2025-01-01 00:00:00" + " " * limit
    yield "2025-01-01 00:00:00 " + "[" * limit
    yield '1.2.3.4 - - [a] "' + "x" * limit
    yield "Mar  1 00:00:00 h " + "a[" * (limit // 2)


def test_builtin_log_patterns_survive_hostile_lines() -> None:
    for pattern in _PATTERNS:
        for line in hostile_lines(_MATCH_PREFIX_CHARS):
            started = time.perf_counter()
            pattern.regex.match(line[:_MATCH_PREFIX_CHARS])
            assert time.perf_counter() - started < 0.05, pattern.name


def test_builtin_email_pattern_survives_hostile_megabyte_lines() -> None:
    email = re.compile(EMAIL_PATTERN)
    for line in hostile_lines(1 << 20):
        started = time.perf_counter()
        list(email.finditer(line))
        assert time.perf_counter() - started < 2.0


CATASTROPHIC = [
    r"(a+)+$", r"(a*)*", r"(.*)*x", r"(\w+\s?)+", r"((ab)+)+", r"(a{1,64})+",
    r"(?:x+)*y", r"(?P<n>a+)+", r"([a-z]+\d*)*", r"(a+){2,}",
]  # fmt: skip
BENIGN = [
    r"(\w+)=(\w+)", r"(\d{4})-(\d\d)-(\d\d)", r"(\d{1,3}\.){3}\d{1,3}", r"[a-z]+@[a-z]+\.com",
    r"(foo|bar)+", r"a{1,64}", r"(a+)", r"(?i)error (\w+)", r"\((\d+)\)+", r"[(a+)+]",
    r"(a?)+", r"x{,5}y", r"\d{3}-\d{4}", r"(ab){3}",
]  # fmt: skip


@pytest.mark.parametrize("pattern", CATASTROPHIC)
def test_nested_unbounded_repeats_are_rejected(pattern: str) -> None:
    with pytest.raises(InvalidConfigurationException, match="upper bound"):
        vet_pattern(pattern)


@pytest.mark.parametrize("pattern", BENIGN)
def test_ordinary_patterns_pass_the_vetting(pattern: str) -> None:
    vet_pattern(pattern)


def test_the_catastrophic_shape_really_is_catastrophic() -> None:
    """Why the vetting exists: 24 'a's and a mismatch already take noticeable time."""
    started = time.perf_counter()
    re.match(r"(a+)+$", "a" * 24 + "!")

    assert time.perf_counter() - started > 0.05


def test_custom_patterns_need_operator_opt_in(env: Env) -> None:
    arguments = {"path": "ok.csv", "output_path": "out.csv", "pattern": r"(\w+)=(\w+)"}

    (refused,) = Client(create_mcp_server([env.workspace])).handshake_and(call(2, "mine_dataset", arguments))

    assert refused["result"]["isError"] is True and "disabled" in tool_text(refused)
    assert not (env.workspace / "out.csv").exists()
    assert not (env.workspace / ".scratch").exists()  # nothing was started


def test_catastrophic_pattern_refused_even_when_enabled(env: Env) -> None:
    arguments = {"path": "ok.csv", "output_path": "out.csv", "pattern": "(a+)+$"}

    (refused,) = Client(create_mcp_server([env.workspace], allow_custom_patterns=True)).handshake_and(
        call(2, "mine_dataset", arguments)
    )

    assert refused["result"]["isError"] is True and "upper bound" in tool_text(refused)
    assert not (env.workspace / "out.csv").exists()
    assert not (env.workspace / ".scratch").exists()


def test_enabling_custom_patterns_logs_a_warning(env: Env) -> None:
    records: list[logging.LogRecord] = []

    class Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("tests.security.patterns")
    logger.addHandler(Collector())
    logger.propagate = False
    create_mcp_server([env.workspace], logger=logger)
    assert records == []  # silent by default

    server = create_mcp_server([env.workspace], allow_custom_patterns=True, logger=logger)
    (listing,) = Client(server).handshake_and(request(2, "tools/list"))

    assert [r.levelno for r in records] == [logging.WARNING]
    assert "hang" in records[0].getMessage()
    mine = next(t for t in listing["result"]["tools"] if t["name"] == "mine_dataset")
    assert "bounded" in mine["inputSchema"]["properties"]["pattern"]["description"]


def test_cli_warning_goes_to_stderr_even_without_logs(env: Env) -> None:
    command = [sys.executable, "-m", "datamining_skill.cli", "mcp", "--allow-dir", str(env.workspace), "--no-logs"]
    init = json.dumps(LEGACY_INIT) + "\n"

    plain = subprocess.run(command, input=init, capture_output=True, text=True, check=False, timeout=60)
    enabled = subprocess.run([*command, "--allow-custom-patterns"], input=init, capture_output=True, text=True, check=False, timeout=60)

    assert "WARNING" not in plain.stderr
    assert CUSTOM_PATTERN_WARNING in enabled.stderr
    assert len(enabled.stdout.splitlines()) == 1 and "WARNING" not in enabled.stdout  # protocol channel stays clean


# ====================================================================== no network, ever


def test_a_full_session_never_opens_a_socket_or_resolves_a_name(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("network access attempted")

    for name in ("socket", "create_connection", "getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "gethostname", "getfqdn"):
        monkeypatch.setattr(socket, name, forbidden)

    create_profiler().profile(env.workspace / "ok.csv")
    run_mining(env.workspace / "ok.csv", env.workspace / "out.csv", workspace=env.workspace)
    server = create_mcp_server([env.workspace])
    responses = Client(server).exchange(
        LEGACY_INIT, INITIALIZED,
        call(2, "profile_dataset", {"path": "sub/data.csv"}),
        request(3, "server/discover", {"_meta": MODERN_META}),
    )  # fmt: skip

    assert len(responses) == 3 and "error" not in responses[1]


NETWORK_MODULES = {
    "socket", "ssl", "http", "urllib", "ftplib", "smtplib", "poplib", "imaplib", "telnetlib", "xmlrpc",
    "asyncio", "socketserver", "selectors", "webbrowser", "subprocess", "multiprocessing", "pickle",
    "shelve", "xml", "importlib.metadata", "ensurepip",
}  # fmt: skip


def test_no_network_capable_module_is_even_loaded(env: Env) -> None:
    probe = (
        "import io, json, sys\n"
        "from pathlib import Path\n"
        "from datamining_skill import create_profiler, create_mcp_server, run_mining\n"
        f"ws = Path({str(env.workspace)!r})\n"
        "create_profiler().profile(ws / 'ok.csv')\n"
        "run_mining(ws / 'ok.csv', ws / 'probe.csv', workspace=ws)\n"
        "msgs = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18'}},\n"
        "        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'profile_dataset', 'arguments': {'path': 'ok.csv'}}}]\n"
        "create_mcp_server([ws]).serve(io.StringIO('\\n'.join(map(json.dumps, msgs)) + '\\n'), io.StringIO())\n"
        f"print(json.dumps(sorted(m for m in sys.modules if m in {sorted(NETWORK_MODULES)!r} or m.split('.')[0] in {sorted(NETWORK_MODULES)!r})))\n"
    )

    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=False, timeout=120)

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []


def runtime_trees() -> list[tuple[Path, ast.Module]]:
    return [(path, ast.parse(path.read_text(encoding="utf-8"))) for path in sorted(SRC.rglob("*.py"))]


def test_runtime_imports_only_the_standard_library() -> None:
    stdlib = set(sys.stdlib_module_names)
    foreign: list[str] = []
    for path, tree in runtime_trees():
        for node in ast.walk(tree):
            names = (
                [alias.name for alias in node.names] if isinstance(node, ast.Import)
                else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0
                else []
            )  # fmt: skip
            foreign += [f"{path.name}: {n}" for n in names if n.split(".")[0] not in stdlib | {"datamining_skill"}]
    assert foreign == []
    project = tomllib.loads((SRC.parent.parent / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert project["dependencies"] == []
    assert set(project["optional-dependencies"]) == {"dev"}


def test_runtime_has_no_network_process_or_dynamic_code_imports() -> None:
    banned = {m.split(".")[0] for m in NETWORK_MODULES} | {"marshal", "importlib", "uuid", "platform", "getpass", "code", "codeop", "runpy"}
    found = [
        f"{path.name}:{getattr(node, 'lineno', 0)}: {name}"
        for path, tree in runtime_trees()
        for node in ast.walk(tree)
        for name in (
            [a.name for a in node.names] if isinstance(node, ast.Import)
            else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0
            else []
        )  # fmt: skip
        if name.split(".")[0] in banned
    ]
    assert found == []


def test_runtime_never_evaluates_data_or_spawns_programs() -> None:
    os_calls = {"system", "popen", "startfile", "execv", "execl", "execvp", "execlp", "spawnl", "spawnv", "fork", "kill"}
    network_calls = {"gethostbyname", "getaddrinfo", "create_connection", "urlopen", "getnameinfo", "gethostname", "getfqdn", "socket"}
    found: list[str] = []
    for path, tree in runtime_trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Name) and function.id in {"eval", "exec", "compile", "__import__"}:
                found.append(f"{path.name}:{node.lineno}: {function.id}()")
            if isinstance(function, ast.Attribute) and function.attr in os_calls | network_calls and isinstance(function.value, ast.Name) and function.value.id in {"os", "socket", "urllib"}:
                found.append(f"{path.name}:{node.lineno}: {function.value.id}.{function.attr}()")
    assert found == []


def test_no_hard_coded_urls_or_hosts_in_executable_strings() -> None:
    pattern = re.compile(r"https?://|ftp://|\b\d{1,3}(\.\d{1,3}){3}\b|\b[a-z0-9-]+\.(com|net|org|io|dev|ai)\b", re.IGNORECASE)
    found: list[str] = []
    for path, tree in runtime_trees():
        docstrings = {
            id(first.value)
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef))
            and ast.get_docstring(node, clean=False) is not None
            and isinstance((first := node.body[0]), ast.Expr)
        }
        found += [
            f"{path.name}:{node.lineno}: {node.value[:50]!r}"
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings and pattern.search(node.value)
        ]
    assert found == []


def test_native_libraries_limited_to_windows_security_apis() -> None:
    loaded: set[str] = set()
    for _path, tree in runtime_trees():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"WinDLL", "CDLL", "LoadLibrary", "OleDLL", "PyDLL"}:
                assert isinstance(node.args[0], ast.Constant)
                loaded.add(str(node.args[0].value))
    assert loaded <= {"kernel32", "advapi32"}


def test_only_the_stderr_stream_handler_is_ever_installed() -> None:
    text = "\n".join(path.read_text(encoding="utf-8") for path in SRC.rglob("*.py"))
    assert not re.search(r"HTTPHandler|SocketHandler|SMTPHandler|SysLogHandler|DatagramHandler|logging\.handlers", text)


# ========================================================================= SQL safety

SQL_WORDS = re.compile(r"\b(select|insert|update|delete|drop|pragma|where|create table)\b", re.IGNORECASE)


def test_sqlite_is_used_by_exactly_one_module() -> None:
    users = [path.name for path, tree in runtime_trees() if any(
        (isinstance(n, ast.Import) and any(a.name == "sqlite3" for a in n.names)) or (isinstance(n, ast.ImportFrom) and n.module == "sqlite3")
        for n in ast.walk(tree)
    )]  # fmt: skip
    assert users == ["state_manager.py"]


def test_sql_text_is_never_assembled_from_strings() -> None:
    tree = ast.parse((SRC / "infrastructure" / "state_manager.py").read_text(encoding="utf-8"))
    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            literal = "".join(p.value for p in node.values if isinstance(p, ast.Constant) and isinstance(p.value, str))
            if SQL_WORDS.search(literal):
                offenders.append(f"f-string at line {node.lineno}")
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)):
            for side in (node.left, node.right):
                if isinstance(side, ast.Constant) and isinstance(side.value, str) and SQL_WORDS.search(side.value):
                    offenders.append(f"concatenation/percent at line {node.lineno}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "format":
            if isinstance(node.func.value, ast.Constant) and isinstance(node.func.value.value, str) and SQL_WORDS.search(node.func.value.value):
                offenders.append(f".format at line {node.lineno}")
    assert offenders == []


def test_sql_statements_are_literals_with_bound_parameters() -> None:
    tree = ast.parse((SRC / "infrastructure" / "state_manager.py").read_text(encoding="utf-8"))
    constants = {
        target.id: node.value
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    statements = {name: value.value for name, value in constants.items() if SQL_WORDS.search(str(value.value))}
    assert len(statements) >= 20
    executed = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in {"execute", "executemany", "executescript"}:
            first = node.args[0]
            assert isinstance(first, ast.Name) and (first.id in constants or first.id == "sql")
            executed += 1
    assert executed >= 15


def test_hostile_text_in_a_database_path_cannot_reach_sql(state_dir: Path) -> None:
    name = "x'; DROP TABLE chunks; --.sqlite3"
    with StateManager(state_dir / name, allowed_roots=[SCRATCH_ROOT]) as state:
        state.initialize([ChunkMetadata(1, 0, 10)])

        assert state.get(1).status is ChunkStatus.PENDING


# ===================================================== integrity under abrupt termination

KILLABLE_WORKER = """
import sys
from pathlib import Path
from datamining_skill import ChunkMetadata, StateManager
db, root = Path(sys.argv[1]), Path(sys.argv[2])
with StateManager(db, allowed_roots=[root]) as state:
    if not state.is_initialized():
        state.initialize(ChunkMetadata(i, (i - 1) * 100, i * 100) for i in range(1, 3001))
    print("ready", flush=True)
    while (chunk := state.claim_next_pending()) is not None:
        state.mark_completed(chunk.chunk_id, output_end=chunk.chunk_id * 10)
        print(chunk.chunk_id, flush=True)
"""


def test_state_database_survives_kills_at_random_moments(
    state_dir: Path,
) -> None:
    import random

    rng = random.Random(1234)
    database = state_dir / "killed.sqlite3"
    for round_number in range(14):
        process = subprocess.Popen(
            [sys.executable, "-c", KILLABLE_WORKER, str(database), str(SCRATCH_ROOT)],
            stdout=subprocess.PIPE, text=True,
        )  # fmt: skip
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "ready"
        for _ in range(rng.randint(0, 120)):
            process.stdout.readline()
        process.kill()  # TerminateProcess / SIGKILL: no cleanup of any kind
        process.wait(timeout=30)
        process.stdout.close()

        for attempt in range(40):
            try:  # right after a kill the OS (or a virus scanner) may briefly still hold the WAL
                with closing(sqlite3.connect(database)) as raw:
                    verdict = raw.execute("PRAGMA integrity_check").fetchall()
                    commits = raw.execute("SELECT COUNT(*) FROM chunk_commits").fetchone()[0]
                break
            except sqlite3.OperationalError:
                if attempt == 39:
                    raise
                time.sleep(0.05)
        assert verdict == [("ok",)], f"round {round_number}"
        with StateManager(database, allowed_roots=[SCRATCH_ROOT], recover_orphans=False) as observer:
            completed = [r.chunk_id for r in observer.records(ChunkStatus.COMPLETED)]
            counts = observer.summary()
            # atomicity: COMPLETED and its ledger row are written together or not at all
            assert commits == len(completed), f"round {round_number}"
            assert completed == list(range(1, len(completed) + 1))
            assert counts[ChunkStatus.IN_PROGRESS] <= 1
            assert observer.committed_output_end() == (len(completed) * 10 or None)

    final = subprocess.run(
        [sys.executable, "-c", KILLABLE_WORKER, str(database), str(SCRATCH_ROOT)],
        capture_output=True, text=True, check=False, timeout=120,
    )  # fmt: skip
    assert final.returncode == 0
    with StateManager(database, allowed_roots=[SCRATCH_ROOT]) as state:
        assert state.is_complete()
        assert state.committed_output_end() == 3000 * 10


# ============================================================================ permissions


@posix_only
def test_posix_artifacts_are_owner_only_whatever_the_umask(env: Env) -> None:
    previous = os.umask(0)  # the most permissive umask: modes must not depend on it
    try:
        run_mining(env.workspace / "ok.csv", env.workspace / "out.csv", workspace=env.workspace)
        scratch = env.workspace / ".scratch"
        observed = {
            ".scratch": scratch,
            "job scratch dir": next(p for p in scratch.iterdir() if p.is_dir()),
            "state db": next(scratch.glob("*.sqlite3")),
            "output": env.workspace / "out.csv",
        }
        modes = {label: stat.S_IMODE(path.stat().st_mode) for label, path in observed.items()}
        assert modes[".scratch"] == modes["job scratch dir"] == 0o700
        assert modes["state db"] == modes["output"] == 0o600
    finally:
        os.umask(previous)


@posix_only
def test_posix_sqlite_side_files_and_chunk_files_are_owner_only(state_dir: Path) -> None:
    previous = os.umask(0)
    try:
        with StateManager(state_dir / "p" / "state.sqlite3", allowed_roots=[SCRATCH_ROOT]) as state:
            state.initialize([ChunkMetadata(1, 0, 10)])
            state.claim_next_pending()
            siblings = sorted((state_dir / "p").iterdir())
            assert [p.name for p in siblings] == ["state.sqlite3", "state.sqlite3-shm", "state.sqlite3-wal"]
            assert {stat.S_IMODE(p.stat().st_mode) & 0o077 for p in siblings} == {0}
            assert stat.S_IMODE((state_dir / "p").stat().st_mode) == 0o700
        scratch = LocalScratchStore(state_dir / "tmp-area", allowed_roots=[SCRATCH_ROOT])
        with scratch.open_tmp(1):
            pass
        assert stat.S_IMODE(scratch.tmp_path(1).stat().st_mode) == 0o600
        assert stat.S_IMODE(scratch.directory.stat().st_mode) == 0o700
    finally:
        os.umask(previous)


@posix_only
def test_existing_directories_keep_their_permissions(state_dir: Path) -> None:
    existing = state_dir / "shared"
    existing.mkdir()
    existing.chmod(0o755)

    with StateManager(existing / "s.sqlite3", allowed_roots=[SCRATCH_ROOT]):
        pass

    assert stat.S_IMODE(existing.stat().st_mode) == 0o755


def test_nested_output_directories_are_created_private(env: Env) -> None:
    server = create_mcp_server([env.workspace])

    (response,) = Client(server).handshake_and(
        call(2, "mine_dataset", {"path": "ok.csv", "output_path": "a/b/c/out.csv"})
    )

    assert response["result"]["isError"] is False
    if not ON_WINDOWS:
        for directory in (env.workspace / "a", env.workspace / "a" / "b", env.workspace / "a" / "b" / "c"):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((env.workspace / "a" / "b" / "c" / "out.csv").stat().st_mode) == 0o600


def test_private_directory_creates_parents_refuses_files(state_dir: Path) -> None:
    ensure_private_directory(state_dir / "x" / "y" / "z")
    assert (state_dir / "x" / "y" / "z").is_dir()
    ensure_private_directory(state_dir / "x" / "y" / "z")  # idempotent
    blocker = state_dir / "blocker"
    blocker.write_text("not a directory")

    with pytest.raises(OSError):
        ensure_private_directory(blocker)


BROAD_TRUSTEES = re.compile(r";(WD|BU|AU|IU|NU|AN|LG|DU|DG)\)")


@windows_only
def test_windows_artifacts_are_not_readable_by_other_users(env: Env) -> None:
    """The inherited ACL on many volumes grants read to every local user; ours must not."""
    run_mining(env.workspace / "ok.csv", env.workspace / "out.csv", workspace=env.workspace)
    scratch = env.workspace / ".scratch"
    state_file = next(scratch.glob("*.sqlite3"))

    for label, path in {"scratch dir": scratch, "state db": state_file, "output": env.workspace / "out.csv"}.items():
        sddl = describe_dacl(path)
        assert not BROAD_TRUSTEES.search(sddl), f"{label}: {sddl}"
    assert describe_dacl(scratch).startswith("D:P"), "the directory ACL must not inherit from its parent"
    assert describe_dacl(env.workspace / "out.csv").startswith("D:P")


@windows_only
def test_windows_sqlite_side_files_inherit_the_private_acl(state_dir: Path) -> None:
    with StateManager(state_dir / "acl" / "state.sqlite3", allowed_roots=[SCRATCH_ROOT]) as state:
        state.initialize([ChunkMetadata(1, 0, 10)])
        state.claim_next_pending()
        for name in ("state.sqlite3", "state.sqlite3-wal", "state.sqlite3-shm"):
            sddl = describe_dacl(state_dir / "acl" / name)
            assert not BROAD_TRUSTEES.search(sddl), f"{name}: {sddl}"


@windows_only
def test_restricting_a_missing_path_fails_softly(state_dir: Path) -> None:
    assert restrict_to_owner(state_dir / "does-not-exist") is False


# ============================================================== terminal-escape safety


def test_printable_escapes_terminal_control_sequences() -> None:
    assert printable("a\x1b[31mred\x1b[0m") == "a\\x1b[31mred\\x1b[0m"
    assert printable("x\u202ey") == "x\\u202ey"  # bidirectional override
    assert printable("line\nbreak") == "line\\x0abreak"
    assert printable("café ünï 日本") == "café ünï 日本"  # ordinary text is untouched


def test_exception_messages_cannot_forge_terminal_output() -> None:
    hostile = "evil\x1b]0;pwned\x07\u202e.csv"

    unsupported = str(UnsupportedDataFormatException(hostile, "no"))

    assert "\x1b" not in unsupported and "\x07" not in unsupported and "\u202e" not in unsupported
    assert "evil" in unsupported


def test_hostile_file_name_cannot_rewrite_the_terminal(
    state_dir: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = state_dir / "evil\u202egnp.csv"
    path.write_bytes(b"\x00\x01\x02" * 100)

    code = main(["profile", str(path), "--no-logs"])

    err = capsys.readouterr().err
    assert code == 2 and "\u202e" not in err and "\\u202e" in err
