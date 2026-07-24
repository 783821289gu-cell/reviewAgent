from dataclasses import dataclass

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.engine import make_url


CHECKPOINT_SCHEMA = "langgraph"


@dataclass
class ReviewCheckpointManager:
    saver: BaseCheckpointSaver
    backend: str
    _pool: ConnectionPool | None = None

    @classmethod
    def in_memory(cls) -> "ReviewCheckpointManager":
        return cls(saver=InMemorySaver(), backend="memory")

    @classmethod
    def postgres(cls, database_url: str) -> "ReviewCheckpointManager":
        normalized_url = str(database_url).strip()
        if not normalized_url:
            raise ValueError("PostgreSQL database URL is required for checkpointing")
        url = make_url(normalized_url).set(drivername="postgresql")
        conninfo = url.render_as_string(hide_password=False)
        with Connection.connect(conninfo, autocommit=True) as connection:
            connection.execute(
                f'CREATE SCHEMA IF NOT EXISTS "{CHECKPOINT_SCHEMA}"'
            )
        pool = ConnectionPool(
            conninfo=conninfo,
            min_size=1,
            max_size=2,
            open=True,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
            },
            configure=_configure_connection,
            check=ConnectionPool.check_connection,
        )
        try:
            pool.wait()
            saver = PostgresSaver(pool)
            saver.setup()
        except Exception:
            pool.close()
            raise
        return cls(
            saver=saver,
            backend="postgres",
            _pool=pool,
        )

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None


def _configure_connection(connection: Connection) -> None:
    connection.execute(
        f'SET search_path TO "{CHECKPOINT_SCHEMA}", public'
    )
