"""Notify the local Codex desktop after SDK archive operations.

The desktop's internal IPC archive broadcast is version 2. Keep this optional
integration isolated: persisted SDK archives remain valid when the app is absent.
"""

import asyncio
import json
import os
import stat
import struct
import uuid
from pathlib import Path


async def notify_archived(threads):
    """Send archive cache invalidations; this is not a UI-render acknowledgment."""
    if not threads:
        return
    root = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    path = root / "ipc" / "ipc.sock"
    directory, socket = path.parent.lstat(), path.lstat()
    if (
        not stat.S_ISDIR(directory.st_mode)
        or not stat.S_ISSOCK(socket.st_mode)
        or directory.st_uid != os.getuid()
        or socket.st_uid != os.getuid()
        or directory.st_mode & 0o022
    ):
        raise OSError("Codex desktop IPC must be a socket in a private same-user directory")
    async with asyncio.timeout(5):
        reader, writer = await asyncio.open_unix_connection(path)
        try:

            def send(message):
                payload = json.dumps(message).encode()
                writer.write(struct.pack("<I", len(payload)) + payload)

            request = str(uuid.uuid4())
            send(
                {
                    "type": "request",
                    "requestId": request,
                    "sourceClientId": "initializing-client",
                    "version": 0,
                    "method": "initialize",
                    "params": {"clientType": "agentic-harness"},
                }
            )
            await writer.drain()
            while True:
                size = struct.unpack("<I", await reader.readexactly(4))[0]
                if not 0 < size <= 1_048_576:
                    raise ValueError("Invalid Codex desktop IPC frame size")
                response = json.loads(await reader.readexactly(size))
                if response.get("requestId") == request:
                    break
            identity = response.get("result", {}).get("clientId")
            if (
                response.get("resultType") != "success"
                or not isinstance(identity, str)
                or not identity
            ):
                raise ValueError("Codex desktop IPC initialization failed")
            for thread in threads:
                send(
                    {
                        "type": "broadcast",
                        "method": "thread-archived",
                        "version": 2,
                        "sourceClientId": identity,
                        "params": {
                            "hostId": "local",
                            "conversationId": thread["id"],
                            "cwd": thread["cwd"],
                        },
                    }
                )
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
