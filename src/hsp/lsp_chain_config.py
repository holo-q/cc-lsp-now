from __future__ import annotations

from collections.abc import Callable

from hsp.chain_server import ChainServer

EnvLookup = Callable[[str, str], str]


def parse_replace(raw: str) -> dict[str, str]:
    """Parse LSP_REPLACE into a command substitution map."""
    raw = raw.strip()
    if not raw:
        return {}
    result: dict[str, str] = {}
    for entry in raw.split(","):
        entry = entry.strip()
        if "=" not in entry:
            continue
        old, new = entry.split("=", 1)
        old, new = old.strip(), new.strip()
        if old and new:
            result[old] = new
    return result


def parse_chain(env: EnvLookup) -> list[ChainServer]:
    """Build the LSP chain from a route/env lookup.

    This is shared by the MCP frontend and broker so broker-owned route
    resolution uses the same chain semantics as direct mode.
    """
    replace = parse_replace(env("LSP_REPLACE", ""))

    def sub(cmd: str) -> str:
        return replace.get(cmd, cmd)

    servers_env = env("LSP_SERVERS", "").strip()
    if servers_env:
        chain: list[ChainServer] = []
        for i, entry in enumerate(s.strip() for s in servers_env.split(";")):
            if not entry:
                continue
            tokens = entry.split()
            cmd, args = sub(tokens[0]), tokens[1:]
            label = cmd if i == 0 else f"{cmd} (fallback{f' {i}' if i > 1 else ''})"
            chain.append(ChainServer(command=cmd, args=args, name=cmd, label=label))
        if not chain:
            raise ValueError("LSP_SERVERS is empty or malformed")
        return chain

    primary_cmd = env("LSP_COMMAND", "")
    if not primary_cmd:
        raise ValueError("LSP_SERVERS or LSP_COMMAND environment variable is required")
    primary_cmd = sub(primary_cmd)

    chain = [
        ChainServer(
            command=primary_cmd,
            args=env("LSP_ARGS", "").split() if env("LSP_ARGS", "") else [],
            name=primary_cmd,
            label=primary_cmd,
        )
    ]

    first_fb = env("LSP_FALLBACK_COMMAND", "")
    if first_fb:
        first_fb = sub(first_fb)
        chain.append(
            ChainServer(
                command=first_fb,
                args=env("LSP_FALLBACK_ARGS", "").split() if env("LSP_FALLBACK_ARGS", "") else [],
                name=first_fb,
                label=f"{first_fb} (fallback)",
            )
        )

    i = 2
    while True:
        cmd = env(f"LSP_FALLBACK_{i}_COMMAND", "")
        if not cmd:
            break
        cmd = sub(cmd)
        chain.append(
            ChainServer(
                command=cmd,
                args=env(f"LSP_FALLBACK_{i}_ARGS", "").split() if env(f"LSP_FALLBACK_{i}_ARGS", "") else [],
                name=cmd,
                label=f"{cmd} (fallback {i})",
            )
        )
        i += 1

    return chain


def parse_prefer(env: EnvLookup, chain: list[ChainServer]) -> dict[str, int]:
    """Parse LSP_PREFER into a method-to-chain-index map."""
    prefer_env = env("LSP_PREFER", "").strip()
    if not prefer_env:
        return {}
    replace = parse_replace(env("LSP_REPLACE", ""))
    result: dict[str, int] = {}
    for entry in prefer_env.split(","):
        entry = entry.strip()
        if "=" not in entry:
            continue
        method, cmd = entry.split("=", 1)
        method, cmd = method.strip(), replace.get(cmd.strip(), cmd.strip())
        for idx, cfg in enumerate(chain):
            if cfg.command == cmd:
                result[method] = idx
                break
    return result
