"""Versioned desktop notifications over a real local socket, without app mutation."""

import asyncio
import json
import struct
import tempfile
from pathlib import Path

import pytest

from backlog_harness.desktop import notify_archived


def test_empty_replay_never_connects(monkeypatch):
    monkeypatch.setenv("CODEX_HOME", "/does-not-exist")
    asyncio.run(notify_archived([]))


def test_missing_app_is_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(OSError):
        asyncio.run(notify_archived([{"id": "old", "cwd": "/project"}]))


def test_non_socket_path_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "ipc").mkdir()
    (tmp_path / "ipc/ipc.sock").touch()
    with pytest.raises(OSError, match="private same-user"):
        asyncio.run(notify_archived([{"id": "old", "cwd": "/project"}]))


@pytest.mark.parametrize("mode", ["success", "rejected", "bad-frame", "disconnect"])
def test_socket_handshake_and_broadcast(monkeypatch, mode):
    async def scenario(root):
        received = []
        finished = asyncio.Event()
        path = root / "ipc"
        path.mkdir(mode=0o700)

        async def read(reader):
            size = struct.unpack("<I", await reader.readexactly(4))[0]
            return json.loads(await reader.readexactly(size))

        async def serve(reader, writer):
            try:
                request = await read(reader)
                assert request["method"] == "initialize"
                assert request["params"] == {"clientType": "agentic-harness"}
                if mode == "disconnect":
                    return
                if mode == "bad-frame":
                    writer.write(struct.pack("<I", 0))
                    await writer.drain()
                    return
                for response in [
                    {"type": "broadcast", "method": "client-status-changed"},
                    {
                        "requestId": request["requestId"],
                        "resultType": "error" if mode == "rejected" else "success",
                        "result": {"clientId": "assigned-client"},
                    },
                ]:
                    payload = json.dumps(response).encode()
                    writer.write(struct.pack("<I", len(payload)) + payload)
                await writer.drain()
                if mode == "success":
                    received.append(await read(reader))
            finally:
                writer.close()
                await writer.wait_closed()
                finished.set()

        server = await asyncio.start_unix_server(serve, path / "ipc.sock")
        async with server:
            if mode == "success":
                await notify_archived([{"id": "old", "cwd": "/project"}])
            else:
                with pytest.raises((ValueError, EOFError)):
                    await notify_archived([{"id": "old", "cwd": "/project"}])
            await asyncio.wait_for(finished.wait(), 2)
        if mode == "success":
            assert received == [
                {
                    "type": "broadcast",
                    "method": "thread-archived",
                    "version": 2,
                    "sourceClientId": "assigned-client",
                    "params": {"hostId": "local", "conversationId": "old", "cwd": "/project"},
                }
            ]

    with tempfile.TemporaryDirectory(dir="/tmp") as folder:
        monkeypatch.setenv("CODEX_HOME", folder)
        asyncio.run(scenario(Path(folder)))
