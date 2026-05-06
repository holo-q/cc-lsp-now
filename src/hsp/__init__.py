from __future__ import annotations


def main(argv: list[str] | None = None) -> None:
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    from hsp.cli import main as cli_main

    raise SystemExit(cli_main(args))


def mcp_main() -> None:
    from hsp.server import run

    run()
