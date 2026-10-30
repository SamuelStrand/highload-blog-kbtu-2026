"""Manage an isolated PostgreSQL cluster inside this checkout."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import subprocess
import tempfile
from pathlib import Path

import asyncpg
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values

from blog_service.certificates import generate_certificates
from blog_service.core.config import Settings

DATABASE_NAMES = {"demo": "sis2_blog", "test": "sis2_blog_test", "bench": "sis2_blog_bench"}


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def ensure_environment() -> Settings:
    root = project_root()
    env_file = root / ".env"
    existing = dotenv_values(env_file) if env_file.exists() else {}
    defaults = {
        "PG_PORT": "55432",
        "PG_USER": "sis2",
        "PG_PASSWORD": secrets.token_urlsafe(32),
        "JWT_SECRET": secrets.token_urlsafe(48),
    }
    missing = {key: value for key, value in defaults.items() if not existing.get(key)}
    if missing:
        with env_file.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write("\n# Local SIS2 settings; do not commit this file.\n")
            for key, value in missing.items():
                stream.write(f"{key}={value}\n")
        env_file.chmod(0o600)
    (root / ".local").mkdir(exist_ok=True)
    return Settings.load()


def _binary(name: str) -> str:
    explicit_directory = os.environ.get("POSTGRES_BIN")
    if explicit_directory:
        candidate = Path(explicit_directory) / (name + ".exe" if os.name == "nt" else name)
        if candidate.is_file():
            return str(candidate)
    located = shutil.which(name)
    if located:
        return located
    raise RuntimeError(f"PostgreSQL program {name!r} was not found; add its bin directory to PATH")


def _run(arguments: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    # A detached PostgreSQL child can inherit a PIPE on Windows. Waiting for its
    # EOF would hang setup for the server's entire lifetime. Capture to a file.
    with tempfile.TemporaryFile() as output:
        completed = subprocess.run(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=output,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            timeout=60,
            check=False,
        )
        output.seek(0)
        result = subprocess.CompletedProcess(
            arguments, completed.returncode, output.read().decode("utf-8", errors="replace"), ""
        )
    if check and result.returncode:
        # Arguments never contain credentials. initdb receives its password via a temporary file.
        raise RuntimeError(f"{Path(arguments[0]).name} failed:\n{result.stdout}\n{result.stderr}")
    return result


def _cluster_paths(settings: Settings) -> tuple[Path, Path]:
    local = (settings.root / ".local").resolve()
    data = (local / "pgdata").resolve()
    if data.parent != local or local.parent != settings.root.resolve():
        raise RuntimeError(
            "PostgreSQL data path must remain inside this checkout's .local directory"
        )
    return data, local / "postgres-cluster.json"


def _validate_marker(settings: Settings, marker: Path, data: Path) -> None:
    expected = {"data": str(data), "port": settings.pg_port, "user": settings.pg_user}
    if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != expected:
        raise RuntimeError(
            "Refusing to control a cluster without matching project ownership metadata"
        )


def start_database() -> None:
    settings = ensure_environment()
    data, marker = _cluster_paths(settings)
    if not (data / "PG_VERSION").is_file():
        if data.exists() and any(data.iterdir()):
            raise RuntimeError(
                f"Refusing to initialize a nonempty PostgreSQL data directory: {data}"
            )
        password_file = settings.root / ".local" / "initdb-password.tmp"
        password_file.write_text(settings.pg_password + "\n", encoding="utf-8")
        password_file.chmod(0o600)
        try:
            _run(
                [
                    _binary("initdb"),
                    "-D",
                    str(data),
                    "-U",
                    settings.pg_user,
                    "--encoding=UTF8",
                    "--locale=C",
                    "--auth-host=scram-sha-256",
                    "--auth-local=scram-sha-256",
                    f"--pwfile={password_file}",
                ]
            )
        finally:
            password_file.unlink(missing_ok=True)
        with (data / "postgresql.conf").open("a", encoding="utf-8") as stream:
            stream.write(
                "\n# SIS2 isolated local cluster\n"
                "listen_addresses = '127.0.0.1'\n"
                f"port = {settings.pg_port}\n"
                "password_encryption = 'scram-sha-256'\n"
                "timezone = 'UTC'\n"
                "max_connections = 100\n"
            )
        marker.write_text(
            json.dumps({"data": str(data), "port": settings.pg_port, "user": settings.pg_user}),
            encoding="utf-8",
        )
    _validate_marker(settings, marker, data)
    status = _run([_binary("pg_ctl"), "-D", str(data), "status"], check=False)
    if status.returncode != 0:
        _run(
            [
                _binary("pg_ctl"),
                "-D",
                str(data),
                "-l",
                str(settings.root / ".local" / "postgres.log"),
                "-w",
                "-t",
                "30",
                "start",
            ]
        )
    print(f"Project PostgreSQL is running on 127.0.0.1:{settings.pg_port}")


def stop_database() -> None:
    settings = Settings.load()
    data, marker = _cluster_paths(settings)
    if not (data / "PG_VERSION").is_file():
        print("Project PostgreSQL has not been initialized")
        return
    _validate_marker(settings, marker, data)
    status = _run([_binary("pg_ctl"), "-D", str(data), "status"], check=False)
    if status.returncode == 0:
        _run([_binary("pg_ctl"), "-D", str(data), "-m", "fast", "-w", "stop"])
    print("Project PostgreSQL is stopped")


async def create_databases(settings: Settings) -> None:
    connection = await asyncpg.connect(
        host="127.0.0.1",
        port=settings.pg_port,
        user=settings.pg_user,
        password=settings.pg_password,
        database="postgres",
        timeout=5,
    )
    try:
        for database_name in DATABASE_NAMES.values():
            exists = await connection.fetchval(
                "SELECT 1 FROM pg_database WHERE datname = $1", database_name
            )
            if not exists:
                # Identifiers come exclusively from the fixed allowlist above.
                await connection.execute(f'CREATE DATABASE "{database_name}"')
    finally:
        await connection.close()


def migrate_database(database: str = "demo") -> None:
    if database not in DATABASE_NAMES:
        raise ValueError("Unknown project database")
    settings = Settings.load(database)
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = settings.database_url
    try:
        config = Config(str(settings.root / "alembic.ini"))
        config.set_main_option("script_location", str(settings.root / "migrations"))
        config.set_main_option("sqlalchemy.url", settings.database_url.replace("%", "%%"))
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    print(f"Migrations applied to {DATABASE_NAMES[database]}")


def setup_environment() -> None:
    settings = ensure_environment()
    generate_certificates(settings.root)
    start_database()
    asyncio.run(create_databases(settings))
    for database in DATABASE_NAMES:
        migrate_database(database)
    from blog_service.seed import seed_database

    manifest = asyncio.run(seed_database(settings, users=3, posts=30, comments=60))
    print("Local setup complete; TLS CA and secrets are stored in ignored project files.")
    print(json.dumps(manifest, indent=2))
