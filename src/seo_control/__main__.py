"""Command-line entry point for the SEO keyword-discovery workspace."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from seo_control.infrastructure.database import initialize_database
from seo_control.web import create_server


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="seo_control")
    subcommands = parser.add_subparsers(dest="command", required=True)

    initialize = subcommands.add_parser("init-db", help="Create or migrate the SQLite database.")
    initialize.add_argument("--database", required=True, help="Path to the SQLite database file.")

    serve = subcommands.add_parser("serve", help="Start the local keyword-discovery web workspace.")
    serve.add_argument("--host", default="127.0.0.1", help="Host to bind.")
    serve.add_argument("--port", default=8000, type=int, help="TCP port to bind (default: 8000).")
    serve.add_argument(
        "--database",
        default="data/seo-control.sqlite3",
        help="SQLite database path (default: data/seo-control.sqlite3).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "init-db":
        connection = initialize_database(arguments.database)
        connection.close()
        print(f"Database initialized: {arguments.database}")
        return 0
    if arguments.command == "serve":
        server = create_server(
            host=arguments.host, port=arguments.port, database_path=arguments.database
        )
        host, port = server.server_address[:2]
        print(f"Keyword discovery workspace: http://{host}:{port}/")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nKeyword discovery workspace stopped.")
        finally:
            server.server_close()
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
