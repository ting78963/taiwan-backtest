import os
import logging
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "")
logger = logging.getLogger(__name__)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=10,
    connect_args={"options": "-c timezone=Asia/Taipei"},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """啟動時執行：建立所有 table + 插入初始版本資料"""
    schema_path = os.path.join(os.path.dirname(__file__), "schema.sql")
    with open(schema_path, "r", encoding="utf-8") as f:
        sql = f.read()
    with engine.connect() as conn:
        conn.execute(text(sql))
        conn.commit()
    logger.info("[DB] Schema initialized / verified.")


def get_current_engine_versions(db) -> dict:
    """取得目前各 engine 的 current 版本號"""
    rows = db.execute(text("""
        SELECT engine_type, version_tag FROM engine_versions WHERE is_current = TRUE
    """)).fetchall()
    return {r[0]: r[1] for r in rows}
