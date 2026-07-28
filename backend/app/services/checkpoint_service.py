"""这里给 LangGraph 准备“暂停书签”。

例子：系统处理到人工复核后暂停。用户第二天回来时，LangGraph 要知道上次停在
哪个节点。Checkpointer 就负责保存和读取这个位置。

数据库中有两个独立区域：

* ``app``：保存合同、风险和反馈，像任务档案柜。
* ``langgraph``：保存暂停位置，像书签盒。

书签丢了，不代表合同丢了。合同内容也不会塞进书签里。
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
    """管理书签存储，以及它占用的数据库连接。"""

    # saver 是 LangGraph 真正调用的书签读写器。
    saver: BaseCheckpointSaver
    # backend 只用于说明当前书签放在内存还是 PostgreSQL。
    backend: str
    # _pool 保存可复用的数据库连接。内存模式没有连接池。
    _pool: ConnectionPool | None = None

    @classmethod
    def in_memory(cls) -> "ReviewCheckpointManager":
        """把书签放在内存里，只适合测试。

        程序一关闭，书签就消失。因此不能用它测试重启恢复。
        """

        return cls(saver=InMemorySaver(), backend="memory")

    @classmethod
    def postgres(cls, database_url: str) -> "ReviewCheckpointManager":
        """把书签放进 PostgreSQL，服务重启后仍能读取。"""

        # normalized_url：去掉配置两端的空格。
        normalized_url = str(database_url).strip()
        if not normalized_url:
            raise ValueError("PostgreSQL database URL is required for checkpointing")
        # conninfo：psycopg 能直接使用的数据库连接字符串。
        url = make_url(normalized_url).set(drivername="postgresql")
        conninfo = url.render_as_string(hide_password=False)
        # schema 可以理解成 PostgreSQL 里的文件夹。
        # 先创建 langgraph 文件夹，避免书签表和业务表混在一起。
        with Connection.connect(conninfo, autocommit=True) as connection:
            connection.execute(
                f'CREATE SCHEMA IF NOT EXISTS "{CHECKPOINT_SCHEMA}"'
            )
        # 最少保留 1 条连接，最多使用 2 条。人工恢复不需要大连接池。
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
            # 确认连接池可用，再把它交给官方 PostgresSaver。
            pool.wait()
            saver = PostgresSaver(pool)
            # 首次启动时创建书签表。重复调用不会重复创建。
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
        """Agent 关闭时释放数据库连接。内存模式不需要处理。"""

        if self._pool is not None:
            self._pool.close()
            self._pool = None


def _configure_connection(connection: Connection) -> None:
    """让每条连接默认在 langgraph 区域中查找和创建书签表。"""

    connection.execute(
        f'SET search_path TO "{CHECKPOINT_SCHEMA}", public'
    )
