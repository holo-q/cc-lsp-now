"""Directly drive pylance to isolate where it dies during willRenameFiles."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path


async def probe():
    root = Path(__file__).parent / "foo_run"
    root_uri = root.resolve().as_uri()
    helper = (root / "src" / "foo_pkg" / "helper.py").resolve()
    helper_uri = helper.as_uri()

    # Spawn pylance, capture stderr raw
    p = await asyncio.create_subprocess_exec(
        "pylance-language-server", "--stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # stderr reader task
    async def read_stderr():
        while True:
            line = await p.stderr.readline()
            if not line:
                print("[stderr] EOF", flush=True)
                return
            print(f"[stderr] {line.decode().rstrip()}", flush=True)

    stderr_task = asyncio.create_task(read_stderr())

    def send(msg):
        body = json.dumps(msg).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode()
        p.stdin.write(header + body)

    async def recv():
        headers = {}
        while True:
            line = await p.stdout.readline()
            if not line:
                return None
            line = line.decode().strip()
            if not line:
                break
            k, v = line.split(":", 1)
            headers[k.strip()] = v.strip()
        n = int(headers["Content-Length"])
        body = await p.stdout.readexactly(n)
        return json.loads(body)

    async def recv_response(target_id):
        while True:
            m = await recv()
            if m is None:
                return None
            if m.get("id") == target_id:
                return m
            print(f"[notif/req from server] method={m.get('method')}", flush=True)

    # Initialize with our full capability block
    init_params = {
        "processId": os.getpid(),
        "rootUri": root_uri,
        "rootPath": str(root.resolve()),
        "capabilities": {
            "textDocument": {},
            "workspace": {
                "workspaceFolders": True,
                "fileOperations": {
                    "dynamicRegistration": False,
                    "willRename": True,
                    "willCreate": True,
                    "willDelete": True,
                },
            },
        },
        "workspaceFolders": [{"uri": root_uri, "name": "foo"}],
    }
    print(">>> initialize", flush=True)
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init_params})
    resp = await recv_response(1)
    caps = resp["result"].get("capabilities", {})
    print("<<< initialized, fileOperations:", caps.get("workspace", {}).get("fileOperations"), flush=True)

    send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    # Open helper.py
    text = helper.read_text()
    print(">>> didOpen helper.py", flush=True)
    send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
        "textDocument": {"uri": helper_uri, "languageId": "python", "version": 0, "text": text}
    }})

    await asyncio.sleep(1.0)  # let pylance index

    # Now send willRenameFiles
    print(">>> willRenameFiles", flush=True)
    new_uri = (root / "src" / "foo_pkg" / "helpers" / "helper.py").resolve().as_uri()
    send({"jsonrpc": "2.0", "id": 2, "method": "workspace/willRenameFiles", "params": {
        "files": [{"oldUri": helper_uri, "newUri": new_uri}]
    }})

    try:
        resp = await asyncio.wait_for(recv_response(2), timeout=30.0)
        print("<<< willRenameFiles response:", flush=True)
        print(json.dumps(resp, indent=2)[:2000], flush=True)
    except asyncio.TimeoutError:
        print("!!! TIMEOUT waiting for willRenameFiles response", flush=True)
    except Exception as e:
        print(f"!!! EXCEPTION: {e}", flush=True)

    # Give stderr a moment to flush
    await asyncio.sleep(2.0)

    # Kill
    p.terminate()
    try:
        await asyncio.wait_for(p.wait(), timeout=3.0)
    except asyncio.TimeoutError:
        p.kill()
    stderr_task.cancel()
    try:
        await stderr_task
    except asyncio.CancelledError:
        pass

    print(f"pylance exit code: {p.returncode}", flush=True)


if __name__ == "__main__":
    # Ensure foo_run exists
    foo_src = Path(__file__).parent / "foo"
    foo_run = Path(__file__).parent / "foo_run"
    import shutil
    if foo_run.exists():
        shutil.rmtree(foo_run)
    shutil.copytree(foo_src, foo_run)
    asyncio.run(probe())
