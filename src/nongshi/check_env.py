"""验证本地环境；不访问天气、模型或项目业务数据库。"""

import importlib
import json
import platform
import sqlite3
import sys
from contextlib import closing
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory

from nongshi.config import Settings


def check_environment() -> dict[str, object]:
    packages = {}
    for module_name, package_name in (
        ("streamlit", "streamlit"),
        ("httpx", "httpx"),
        ("pydantic", "pydantic"),
        ("pydantic_settings", "pydantic-settings"),
    ):
        importlib.import_module(module_name)
        packages[package_name] = version(package_name)

    with TemporaryDirectory(prefix="nongshi-check-") as temporary:
        database = Path(temporary) / "check.sqlite3"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE probe (value TEXT NOT NULL)")
            connection.execute("INSERT INTO probe VALUES (?)", ("nongshi",))
        with closing(sqlite3.connect(database)) as connection:
            row = connection.execute("SELECT value FROM probe").fetchone()
        if row != ("nongshi",):
            raise RuntimeError("SQLite persistence check failed")

    settings = Settings()
    return {
        "python": platform.python_version(),
        "executable": sys.executable,
        "virtual_environment": sys.prefix != sys.base_prefix,
        "packages": packages,
        "sqlite_version": sqlite3.sqlite_version,
        "sqlite_write_read": "ok",
        "model_fields_filled": bool(
            settings.llm_provider.strip()
            and settings.llm_model.strip()
            and settings.llm_api_key.get_secret_value().strip()
        ),
        "external_services_tested": False,
    }


def main() -> None:
    print(json.dumps(check_environment(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
