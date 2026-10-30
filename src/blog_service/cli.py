"""Command-line entry point for local development and SIS2 evidence."""

from __future__ import annotations

import argparse
import asyncio
import json
import ssl
import sys
from uuid import UUID

from blog_service.core.config import Settings


async def verify_protocols(base_url: str, settings: Settings) -> list[dict[str, str | int]]:
    import httpx

    context = ssl.create_default_context(cafile=str(settings.ca_file))
    results = []
    for http2, expected in [(False, "HTTP/1.1"), (True, "HTTP/2")]:
        async with httpx.AsyncClient(
            http2=http2, verify=context, trust_env=False, timeout=5
        ) as client:
            response = await client.get(f"{base_url.rstrip('/')}/health")
            response.raise_for_status()
            if response.http_version != expected:
                raise RuntimeError(f"Expected {expected}, negotiated {response.http_version}")
            results.append({"http_version": response.http_version, "status": response.status_code})
    return results


async def serve_application(database: str, port: int) -> None:
    from hypercorn.asyncio import serve
    from hypercorn.config import Config

    from blog_service.main import create_app

    settings = Settings.load(database)
    if not all(
        path.is_file() for path in (settings.cert_file, settings.key_file, settings.ca_file)
    ):
        raise RuntimeError("TLS files are missing; run blogctl setup first")
    config = Config()
    config.bind = [f"127.0.0.1:{port}"]
    config.certfile = str(settings.cert_file)
    config.keyfile = str(settings.key_file)
    config.alpn_protocols = ["h2", "http/1.1"]
    config.workers = 1
    config.accesslog = None
    config.errorlog = "-"
    config.h2_max_concurrent_streams = 100
    config.keep_alive_timeout = 30
    # Keep connection rotation outside a benchmark trial (5,200 requests).
    # Apply the same limit to HTTP/1.1 and HTTP/2.
    config.keep_alive_max_requests = 100_000
    await serve(create_app(settings), config)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="blogctl", description="SIS2 Blog Platform tools")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("setup", help="Initialize the isolated database, TLS and demo dataset")
    commands.add_parser("db-start", help="Start the project-owned PostgreSQL server")
    commands.add_parser("db-stop", help="Stop the project-owned PostgreSQL server")
    migrate = commands.add_parser("migrate", help="Apply database migrations")
    migrate.add_argument("--database", choices=("demo", "test", "bench"), default="demo")
    seed = commands.add_parser("seed", help="Insert deterministic fixtures")
    seed.add_argument("--database", choices=("demo", "test", "bench"), default="demo")
    seed.add_argument("--reset", action="store_true", help="Reset test/bench data before seeding")
    seed.add_argument("--users", type=int, default=100)
    seed.add_argument("--posts", type=int, default=10000)
    seed.add_argument("--comments", type=int, default=20000)
    token = commands.add_parser("token", help="Issue a local demonstration JWT")
    token.add_argument("--database", choices=("demo", "test", "bench"), default="demo")
    token.add_argument("--user-id", type=UUID)
    token.add_argument("--expires-seconds", type=int, default=3600)
    server = commands.add_parser("serve", help="Serve the API using TLS and HTTP/1.1 + HTTP/2")
    server.add_argument("--database", choices=("demo", "test", "bench"), default="demo")
    server.add_argument("--port", type=int, default=8443)
    verify = commands.add_parser("verify-protocols", help="Check actual HTTP protocol negotiation")
    verify.add_argument("--base-url", default="https://localhost:8443")
    commands.add_parser("benchmark", help="Run the HTTP/1.1 versus HTTP/2 benchmark")
    commands.add_parser("demo", help="Demonstrate idempotency and timeout/retry behavior")
    return parser


def main(argv: list[str] | None = None) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if arguments and arguments[0] in {"benchmark", "demo"}:
        if arguments[0] == "benchmark":
            from blog_service.benchmark import main as delegated_main
        else:
            from blog_service.demo import main as delegated_main

        delegated_main(arguments[1:])
        return
    parser = build_parser()
    args = parser.parse_args(arguments)
    try:
        if args.command in {"setup", "db-start", "db-stop", "migrate"}:
            from blog_service import local

            if args.command == "setup":
                local.setup_environment()
            elif args.command == "db-start":
                local.start_database()
            elif args.command == "db-stop":
                local.stop_database()
            else:
                local.migrate_database(args.database)
        elif args.command == "seed":
            from blog_service.seed import seed_database

            result = asyncio.run(
                seed_database(
                    Settings.load(args.database),
                    reset=args.reset,
                    users=args.users,
                    posts=args.posts,
                    comments=args.comments,
                )
            )
            print(json.dumps(result, indent=2))
        elif args.command == "token":
            from blog_service.seed import DEMO_USER_ID
            from blog_service.users.auth import issue_token

            print(
                issue_token(
                    args.user_id or DEMO_USER_ID,
                    Settings.load(args.database),
                    expires_seconds=args.expires_seconds,
                )
            )
        elif args.command == "serve":
            asyncio.run(serve_application(args.database, args.port))
        elif args.command == "verify-protocols":
            print(
                json.dumps(asyncio.run(verify_protocols(args.base_url, Settings.load())), indent=2)
            )
    except (RuntimeError, ValueError, OSError) as error:
        parser.exit(1, f"Error: {error}\n")


if __name__ == "__main__":
    main()
