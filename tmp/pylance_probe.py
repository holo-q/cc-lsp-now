"""Directly drive pylance to isolate where it dies during willRenameFiles.

v2: adds documentSymbol + references to reproduce the roundtrip's actual flow,
and handles server-to-client requests (registerCapability, workspace/configuration)
with reasonable responses instead of ignoring them.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from pathlib import Path


async def probe():
    root = Path(__file__).parent / "foo_run"
    root_uri = root.resolve().as_uri()
    helper = (root / "src" / "foo_pkg" / "helper.py").resolve()
    helper_uri = helper.as_uri()
    main_py = (root / "src" / "foo_pkg" / "main.py").resolve()
    main_uri = main_py.as_uri()
    other_py = (root / "src" / "foo_pkg" / "other.py").resolve()
    other_uri = other_py.as_uri()

    p = await asyncio.create_subprocess_exec(
        "pylance-language-server", "--stdio",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def read_stderr():
        while True:
            line = await p.stderr.readline()
            if not line:
                print("[stderr] EOF", flush=True)
                return
            decoded = line.decode("utf-8", errors="replace").rstrip()
            if decoded:
                print(f"[stderr] {decoded}", flush=True)

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

    async def recv_response(target_id, *, label=""):
        while True:
            m = await recv()
            if m is None:
                print(f"!!! EOF waiting for id={target_id} ({label})", flush=True)
                return None
            if m.get("id") == target_id and "method" not in m:
                return m
            # Server-to-client request
            if "id" in m and "method" in m:
                method = m["method"]
                req_id = m["id"]
                # Respond sanely per method
                if method == "workspace/configuration":
                    items = m.get("params", {}).get("items", [])
                    result = [{} for _ in items]
                    send({"jsonrpc": "2.0", "id": req_id, "result": result})
                    print(f"[<-req {method} id={req_id}] responded with empty configs for {len(items)} items")
                elif method == "client/registerCapability":
                    send({"jsonrpc": "2.0", "id": req_id, "result": None})
                    print(f"[<-req {method} id={req_id}] acknowledged")
                elif method == "window/workDoneProgress/create":
                    send({"jsonrpc": "2.0", "id": req_id, "result": None})
                    print(f"[<-req {method} id={req_id}] acknowledged")
                else:
                    send({"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "method not found"}})
                    print(f"[<-req {method} id={req_id}] -32601")
                continue
            # Notification
            if "method" in m:
                if m["method"] not in ("telemetry/event", "window/logMessage"):
                    print(f"[<-notif] method={m['method']}")
                continue

    init_params = {
        "processId": os.getpid(),
        "rootUri": root_uri,
        "rootPath": str(root.resolve()),
        "capabilities": {
            "textDocument": {
                "documentSymbol": {
                    "hierarchicalDocumentSymbolSupport": True,
                    "symbolKind": {"valueSet": list(range(1, 27))},
                },
                "references": {},
                "hover": {"contentFormat": ["markdown", "plaintext"]},
                "rename": {"prepareSupport": True},
                "definition": {"linkSupport": True},
                "publishDiagnostics": {"relatedInformation": True},
            },
            "workspace": {
                "workspaceFolders": True,
                "configuration": True,
                "fileOperations": {
                    "dynamicRegistration": False,
                    "willRename": True,
                    "didRename": True,
                    "willCreate": True,
                    "didCreate": True,
                    "willDelete": True,
                    "didDelete": True,
                },
                "workspaceEdit": {
                    "documentChanges": True,
                    "resourceOperations": ["create", "rename", "delete"],
                    "failureHandling": "textOnlyTransactional",
                    "normalizesLineEndings": True,
                    "changeAnnotationSupport": {"groupsOnLabel": True},
                },
            },
        },
        "initializationOptions": {
            "experimentationSupport": False,
            "trustedWorkspaceSupport": True,
            "serverMode": "language-server",
            "diagnosticMode": "openFilesOnly",
        },
        "workspaceFolders": [{"uri": root_uri, "name": "foo"}],
    }
    print(">>> initialize", flush=True)
    send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": init_params})
    resp = await recv_response(1, label="initialize")
    if not resp:
        print("!!! pylance died during initialize")
        return
    caps = resp["result"].get("capabilities", {})
    print(f"<<< initialized; server fileOperations: {caps.get('workspace', {}).get('fileOperations')}", flush=True)

    send({"jsonrpc": "2.0", "method": "initialized", "params": {}})

    # Open all three files
    for uri, path in [(helper_uri, helper), (main_uri, main_py), (other_uri, other_py)]:
        text = path.read_text()
        send({"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {
            "textDocument": {"uri": uri, "languageId": "python", "version": 0, "text": text}
        }})
    print(">>> didOpen (3 files)")

    # Let pylance settle briefly
    await asyncio.sleep(2.0)

    # 1. documentSymbol on helper.py
    print(">>> documentSymbol")
    send({"jsonrpc": "2.0", "id": 2, "method": "textDocument/documentSymbol",
          "params": {"textDocument": {"uri": helper_uri}}})
    try:
        resp = await asyncio.wait_for(recv_response(2, label="documentSymbol"), timeout=15.0)
        if resp:
            result = resp.get("result")
            count = len(result) if isinstance(result, list) else "?"
            print(f"<<< documentSymbol ok ({count} symbols)")
        else:
            print("!!! documentSymbol: server died")
            await asyncio.sleep(1.0)
            return
    except asyncio.TimeoutError:
        print("!!! documentSymbol: TIMEOUT")
        return

    # 2. references on greet in helper.py (line 0 = def greet)
    print(">>> references on greet @ helper.py:1")
    send({"jsonrpc": "2.0", "id": 3, "method": "textDocument/references", "params": {
        "textDocument": {"uri": helper_uri},
        "position": {"line": 0, "character": 4},
        "context": {"includeDeclaration": True},
    }})
    try:
        resp = await asyncio.wait_for(recv_response(3, label="references"), timeout=15.0)
        if resp:
            result = resp.get("result")
            count = len(result) if isinstance(result, list) else "?"
            print(f"<<< references ok ({count} refs)")
        else:
            print("!!! references: server died")
            await asyncio.sleep(1.0)
            return
    except asyncio.TimeoutError:
        print("!!! references: TIMEOUT")
        return

    # 3. willRenameFiles
    print(">>> willRenameFiles")
    new_uri = (root / "src" / "foo_pkg" / "helpers" / "helper.py").resolve().as_uri()
    send({"jsonrpc": "2.0", "id": 4, "method": "workspace/willRenameFiles", "params": {
        "files": [{"oldUri": helper_uri, "newUri": new_uri}]
    }})
    try:
        resp = await asyncio.wait_for(recv_response(4, label="willRenameFiles"), timeout=30.0)
        if resp:
            print("<<< willRenameFiles response:")
            print(json.dumps(resp, indent=2)[:2000])
        else:
            print("!!! willRenameFiles: server died")
    except asyncio.TimeoutError:
        print("!!! willRenameFiles: TIMEOUT")

    await asyncio.sleep(2.0)

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

    print(f"pylance exit code: {p.returncode}")


if __name__ == "__main__":
    foo_src = Path(__file__).parent / "foo"
    foo_run = Path(__file__).parent / "foo_run"
    if foo_run.exists():
        shutil.rmtree(foo_run)
    shutil.copytree(foo_src, foo_run)
    asyncio.run(probe())
