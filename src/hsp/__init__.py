from __future__ import annotations


def main(argv: list[str] | None = None) -> None:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        from hsp.cli import main as cli_main

        raise SystemExit(cli_main(args))

    from hsp.server import mcp

    mcp.run(transport="stdio")
