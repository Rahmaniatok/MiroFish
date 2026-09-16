"""
数据库引擎配置（Universe & Correlation Graph）
SQLModel + SQLite，schema 版本由 Alembic 管理（见 backend/alembic/）
"""

import os

from sqlmodel import Session, create_engine

_INSTANCE_DIR = os.path.join(os.path.dirname(__file__), '../instance')
os.makedirs(_INSTANCE_DIR, exist_ok=True)

GRAPH_DB_PATH = os.path.join(_INSTANCE_DIR, 'universe_graph.db')
GRAPH_DATABASE_URL = os.environ.get('GRAPH_DATABASE_URL', f'sqlite:///{GRAPH_DB_PATH}')

engine = create_engine(GRAPH_DATABASE_URL, echo=False, connect_args={"check_same_thread": False})


def get_session() -> Session:
    """获取一个新的数据库会话（调用方负责 close，推荐用 `with get_session() as s:`）"""
    return Session(engine)
