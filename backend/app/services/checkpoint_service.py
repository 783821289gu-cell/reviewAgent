"""LangGraph Checkpointer 的创建和生命周期管理。

本项目有两类 PostgreSQL 数据，职责不能混淆：

* ``app`` schema 由业务 Repository 管理，保存任务、合同、风险、事件和反馈，
  是对外查询与恢复业务结果的唯一真相源。
* ``langgraph`` schema 由 ``PostgresSaver`` 管理，只保存控制图的执行游标、
  channel 值和 interrupt 信息。

因此删除 Checkpoint 不应删除业务任务，业务状态更新也不能只写 Checkpoint。
"""

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
    """把 Saver 与其连接池绑定在同一个可关闭对象中。

    LangGraph 只要求一个 ``BaseCheckpointSaver``。这里额外保存 backend 名称用于
    状态展示，并保存连接池以确保 Agent 关闭时能释放 PostgreSQL 连接。
    """

    saver: BaseCheckpointSaver
    backend: str
    _pool: ConnectionPool | None = None

    @classmethod
    def in_memory(cls) -> "ReviewCheckpointManager":
        """为单元测试和无持久化场景创建进程内 Checkpointer。

        进程退出后内容会丢失，所以它不能证明“服务重启后仍可恢复”。
        """

        return cls(saver=InMemorySaver(), backend="memory")

    @classmethod
    def postgres(cls, database_url: str) -> "ReviewCheckpointManager":
        """创建可跨 Worker 重启恢复的 PostgreSQL Checkpointer。"""

        normalized_url = str(database_url).strip()
        if not normalized_url:
            raise ValueError("PostgreSQL database URL is required for checkpointing")
        url = make_url(normalized_url).set(drivername="postgresql")
        conninfo = url.render_as_string(hide_password=False)
        # PostgresSaver.setup() 会创建自己的表。先创建独立 schema，再通过每个
        # 连接的 search_path 把这些表隔离到 langgraph schema。
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
            # setup() 是幂等初始化，负责 checkpoints/checkpoint_blobs 等内部表。
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
        """关闭 Manager 拥有的连接池；内存 Saver 无需处理。"""

        if self._pool is not None:
            self._pool.close()
            self._pool = None


def _configure_connection(connection: Connection) -> None:
    """确保连接池中的每条连接都把 LangGraph 内部表写入独立 schema。"""

    connection.execute(
        f'SET search_path TO "{CHECKPOINT_SCHEMA}", public'
    )
