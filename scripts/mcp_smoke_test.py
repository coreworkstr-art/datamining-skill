"""Verify that an installed ``datamining-skill`` answers MCP requests over stdio.

Spawns ``<command> mcp --allow-dir <workspace>`` and drives it like a client would: the legacy
``initialize`` handshake, the modern ``server/discover`` probe, the tool list, a profile, a mine
with streamed progress, a preview of the result, a unique and lower-cased mine, and a background
job followed through ``mining_status``. Exits non-zero (with the reason on stderr) on any mismatch.

Usage::

    python scripts/mcp_smoke_test.py                      # uses the `datamining-skill` command
    python scripts/mcp_smoke_test.py python -m datamining_skill.cli

The workspace is created under ``./.scratch`` and removed afterwards.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, TextIO, cast

MODERN_META = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}
TOOLS = ["profile_dataset", "mine_dataset", "mining_status", "cancel_mining", "preview_result"]


def fail(message: str) -> None:
    print(f"FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def expect(condition: bool, message: str) -> None:
    if not condition:
        fail(message)
    print(f"  ok  {message}")


class Session:
    """One server process, driven request by request."""

    def __init__(self, command: list[str]) -> None:
        self._process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self._next_id = 0
        self.notifications: list[dict[str, Any]] = []

    def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._next_id += 1
        message: dict[str, Any] = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            message["params"] = params
        self._send(message)
        stdout = cast(TextIO, self._process.stdout)
        while True:
            line = stdout.readline()
            if not line:
                fail(f"server closed its output: {self._stderr()[:500]}")
            try:
                reply = json.loads(line)
            except ValueError:
                fail("stdout contained something that is not JSON (the protocol channel must stay clean)")
            if reply.get("id") == self._next_id:
                return cast(dict[str, Any], reply)
            self.notifications.append(reply)

    def notify(self, method: str) -> None:
        self._send({"jsonrpc": "2.0", "method": method})

    def call(self, name: str, arguments: dict[str, Any], **extra: Any) -> dict[str, Any]:
        reply = self.request("tools/call", {"name": name, "arguments": arguments, **extra})
        result = cast(dict[str, Any], reply["result"])
        return result

    def close(self) -> int:
        cast(TextIO, self._process.stdin).close()
        try:
            return self._process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self._process.kill()
            fail("the server did not exit when its input closed")
            return 1

    def _send(self, message: dict[str, Any]) -> None:
        stdin = cast(TextIO, self._process.stdin)
        stdin.write(json.dumps(message) + "\n")
        stdin.flush()

    def _stderr(self) -> str:
        if self._process.poll() is None:
            self._process.kill()
        return cast(TextIO, self._process.stderr).read().strip()


def main() -> int:
    command = sys.argv[1:] or ["datamining-skill"]
    workspace = Path(".scratch") / f"mcp-smoke-{uuid.uuid4().hex[:8]}"
    workspace.mkdir(parents=True)
    try:
        emails = [f"emp{i}@internal.corp.test" for i in range(500)]
        (workspace / "contacts.csv").write_text(
            "id,note\n" + "".join(f"{i},mail {e} now\n" for i, e in enumerate(emails)),
            encoding="utf-8",
            newline="\n",
        )
        (workspace / "repeats.csv").write_text(
            "id,note\n" + "".join(f"{i},mail Emp{i % 10}@Internal.Corp.TEST now\n" for i in range(500)),
            encoding="utf-8",
            newline="\n",
        )

        session = Session([*command, "mcp", "--allow-dir", str(workspace)])
        initialized = session.request(
            "initialize",
            {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke-test", "version": "1"}},
        )
        session.notify("notifications/initialized")
        expect(initialized["result"]["serverInfo"]["name"] == "datamining-skill", "initialize handshake")
        names = [t["name"] for t in session.request("tools/list")["result"]["tools"]]
        expect(names == TOOLS, f"tools/list -> {names}")
        discover = session.request("server/discover", {"_meta": MODERN_META})
        expect("2026-07-28" in discover["result"]["supportedVersions"], "server/discover (modern era)")

        profile = session.call("profile_dataset", {"path": "contacts.csv"})
        expect(profile["structuredContent"]["records"]["count"] == 500, "profile_dataset")

        mined = session.call(
            "mine_dataset",
            {"path": "contacts.csv", "output_path": "found.csv"},
            _meta={"progressToken": "smoke"},
        )
        expect(mined["structuredContent"]["records_written"] == 500, "mine_dataset")
        expect(any(n.get("method") == "notifications/progress" for n in session.notifications), "progress streamed")
        found = (workspace / "found.csv").read_text(encoding="utf-8").splitlines()
        expect(found == ["email", *emails], "result file is exact")

        preview = session.call("preview_result", {"path": "found.csv", "rows": 3})["structuredContent"]
        expect(preview["columns"] == ["email"] and preview["rows"][0] == [emails[0]] and preview["has_more"], "preview_result")

        distinct = session.call(
            "mine_dataset", {"path": "repeats.csv", "output_path": "distinct.csv", "unique": True, "lowercase": True}
        )["structuredContent"]
        expect(distinct["records_written"] == 10 and distinct["duplicates_skipped"] == 490, "unique and lowercase")

        started = session.call(
            "mine_dataset", {"path": "contacts.csv", "output_path": "background.csv", "wait": False}
        )["structuredContent"]
        expect(started["status"] == "running" and bool(started["job_id"]), "background job started")
        deadline = time.monotonic() + 60
        while True:
            status = session.call("mining_status", {"job_id": started["job_id"]})["structuredContent"]
            if status["status"] != "running":
                break
            if time.monotonic() > deadline:
                fail("the background job did not finish in time")
            time.sleep(0.05)
        expect(status["status"] == "succeeded" and status["result"]["records_written"] == 500, "mining_status")

        expect(session.close() == 0, "clean exit when stdin closes")
        print("MCP smoke test passed")
        return 0
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
        try:
            workspace.parent.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
