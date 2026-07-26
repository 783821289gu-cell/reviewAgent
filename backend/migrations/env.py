from logging.config import fileConfig
import os
from pathlib import Path
import sys

from alembic import context
from sqlalchemy import engine_from_config, pool


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = PROJECT_ROOT / "backend" / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from db.postgres_models import Base  # noqa: E402


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.getenv("REVIEW_AGENT_DATABASE_URL", "").strip()
if database_url:
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata
MANAGED_SCHEMAS = {None, "app"}
LANGGRAPH_STORE_TABLES = {
    "store",
    "store_migrations",
    "store_vectors",
    "vector_migrations",
}


def _configured_url() -> str:
    value = config.get_main_option("sqlalchemy.url").strip()
    if not value:
        raise RuntimeError(
            "REVIEW_AGENT_DATABASE_URL is required for Alembic migrations"
        )
    return value


def _include_name(name, type_, parent_names) -> bool:
    if type_ == "schema":
        return name in MANAGED_SCHEMAS
    if (
        type_ == "table"
        and parent_names.get("schema_name") == "app"
        and name in LANGGRAPH_STORE_TABLES
    ):
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=_configured_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        include_name=_include_name,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    _configured_url()
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_schemas=True,
            include_name=_include_name,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
