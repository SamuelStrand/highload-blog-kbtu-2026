"""Environment-backed configuration shared by the API and local tools."""

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from dotenv import dotenv_values

DATABASE_NAMES = {
    "demo": "sis2_blog",
    "test": "sis2_blog_test",
    "bench": "sis2_blog_bench",
}


def project_root() -> Path:
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Settings:
    database_url: str
    jwt_secret: str
    root: Path = field(default_factory=project_root)
    pg_port: int = 55432
    pg_user: str = "sis2"
    pg_password: str = ""
    db_pool_size: int = 20
    cert_file: Path | None = None
    key_file: Path | None = None
    ca_file: Path | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root).resolve())
        cert_dir = self.root / ".local" / "tls"
        for name, default in (
            ("cert_file", "server.pem"),
            ("key_file", "server-key.pem"),
            ("ca_file", "ca.pem"),
        ):
            value = getattr(self, name)
            object.__setattr__(self, name, Path(value) if value else cert_dir / default)

    @classmethod
    def load(cls, database: str = "demo", root: Path | None = None) -> "Settings":
        if database not in DATABASE_NAMES:
            raise ValueError(f"Unknown database profile: {database}")
        root = (root or project_root()).resolve()
        values = {**dotenv_values(root / ".env"), **os.environ}
        password = values.get("PG_PASSWORD", "")
        secret = values.get("JWT_SECRET", "")
        if not password or not secret:
            raise RuntimeError(
                "Run the setup command first: PG_PASSWORD and JWT_SECRET are required."
            )
        port = int(values.get("PG_PORT", "55432"))
        user = values.get("PG_USER", "sis2")
        default_url = (
            f"postgresql+asyncpg://{quote(user, safe='')}:{quote(password, safe='')}"
            f"@127.0.0.1:{port}/{DATABASE_NAMES[database]}"
        )
        return cls(
            database_url=values.get("DATABASE_URL") or default_url,
            jwt_secret=secret,
            root=root,
            pg_port=port,
            pg_user=user,
            pg_password=password,
            db_pool_size=int(values.get("DB_POOL_SIZE", "20")),
        )
