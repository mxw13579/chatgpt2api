from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Column, String, Text, create_engine, Integer, text
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from services.storage.base import StorageBackend

Base = declarative_base()


class AccountModel(Base):
    """账号数据模型"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    # ChatGPT access_token 是 JWT，经常超过 2048。PostgreSQL 的 varchar(2048) 会把导入打成 500。
    # MySQL 不能给整列 TEXT 建唯一索引，所以只在 MySQL 上保留定长字符串。
    access_token = Column(
        Text().with_variant(String(2048), "mysql"),
        unique=True,
        nullable=False,
        index=True,
    )
    data = Column(Text, nullable=False)  # JSON 格式存储完整账号数据


class AuthKeyModel(Base):
    """鉴权密钥数据模型"""
    __tablename__ = "auth_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_id = Column(String(255), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)


class DatabaseStorageBackend(StorageBackend):
    """数据库存储后端（支持 SQLite、PostgreSQL、MySQL 等）"""

    # IN 参数及每批待写入行数有界；兼容旧 SQLite 的 999 参数上限。
    _ACCOUNT_CHANGE_BATCH_SIZE = 400

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,  # 自动检测连接是否有效
            pool_recycle=3600,   # 1小时回收连接
        )
        Base.metadata.create_all(self.engine)
        self._widen_access_token_column()
        self.Session = sessionmaker(bind=self.engine)

    def _widen_access_token_column(self) -> None:
        """把已有库里的 varchar access_token 放成 text。create_all 不会改旧列。"""
        if self.engine.dialect.name != "postgresql":
            return
        with self.engine.begin() as conn:
            row = conn.execute(
                text(
                    """
                    SELECT character_maximum_length
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = 'accounts'
                      AND column_name = 'access_token'
                    """
                )
            ).first()
            if row is None or row[0] is None:
                return
            conn.execute(text("ALTER TABLE accounts ALTER COLUMN access_token TYPE TEXT"))

    def load_accounts(self) -> list[dict[str, Any]]:
        """从数据库加载账号数据"""
        session = self.Session()
        try:
            accounts = []
            for row in session.query(AccountModel).all():
                try:
                    account_data = json.loads(row.data)
                    if isinstance(account_data, dict):
                        accounts.append(account_data)
                except json.JSONDecodeError:
                    continue
            return accounts
        finally:
            session.close()

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存账号数据到数据库"""
        self._save_rows(AccountModel, accounts, "access_token")

    def apply_account_changes(
        self,
        upserts: list[dict],
        deleted_tokens: list[str],
        *,
        snapshot: list[dict] | None = None,
    ) -> None:
        """只读写指定键，所有分块在同一事务内提交（包括 token 轮换）。

        snapshot 仅供全量后端使用；这里只检查其容器类型，不遍历或持久化
        其内容。upsert 与删除同键时保留原行并更新，未变账号的 ID 不变。
        """
        serialized, deletions = self._prepare_account_changes(
            upserts, deleted_tokens, snapshot=snapshot,
        )
        if not serialized and not deletions:
            return

        tokens = list(serialized)
        batch_size = self._ACCOUNT_CHANGE_BATCH_SIZE
        try:
            with self.Session() as session, session.begin():
                for offset in range(0, len(deletions), batch_size):
                    session.query(AccountModel).filter(
                        AccountModel.access_token.in_(deletions[offset:offset + batch_size])
                    ).delete(synchronize_session=False)

                for offset in range(0, len(tokens), batch_size):
                    batch = tokens[offset:offset + batch_size]
                    existing = {
                        row.access_token: row
                        for row in session.query(AccountModel).filter(
                            AccountModel.access_token.in_(batch)
                        ).all()
                    }
                    for token in batch:
                        row = existing.get(token)
                        data = serialized[token]
                        if row is None:
                            session.add(AccountModel(access_token=token, data=data))
                        elif row.data != data:
                            row.data = data
                    # 限制每次 flush 的参数/待写入行数，但不提前 commit。
                    session.flush()
        except Exception:
            # Session 上下文负责回滚；SQLAlchemy 异常常含 token/完整 JSON。
            raise RuntimeError("Failed to apply account changes") from None

    def load_auth_keys(self) -> list[dict[str, Any]]:
        """从数据库加载鉴权密钥数据"""
        return self._load_rows(AuthKeyModel)

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存鉴权密钥数据到数据库"""
        self._save_rows(AuthKeyModel, auth_keys, "id", "key_id")

    def _load_rows(self, model: type[AccountModel] | type[AuthKeyModel]) -> list[dict[str, Any]]:
        session = self.Session()
        try:
            items = []
            for row in session.query(model).all():
                try:
                    item_data = json.loads(row.data)
                    if isinstance(item_data, dict):
                        items.append(item_data)
                except json.JSONDecodeError:
                    continue
            return items
        finally:
            session.close()

    def _save_rows(
        self,
        model: type[AccountModel] | type[AuthKeyModel],
        items: list[dict[str, Any]],
        source_key: str,
        target_key: str | None = None,
    ) -> None:
        session = self.Session()
        try:
            key_column = target_key or source_key
            existing_rows = {
                str(getattr(row, key_column)): row
                for row in session.query(model).all()
            }
            incoming_keys: set[str] = set()

            for item in items:
                if not isinstance(item, dict):
                    continue
                key_value = str(item.get(source_key) or "").strip()
                if not key_value:
                    continue
                if key_value in incoming_keys:
                    raise ValueError(f"Duplicate {source_key} in storage snapshot")

                incoming_keys.add(key_value)
                serialized_data = json.dumps(item, ensure_ascii=False)
                existing_row = existing_rows.get(key_value)
                if existing_row is None:
                    session.add(
                        model(
                            **{key_column: key_value},
                            data=serialized_data,
                        )
                    )
                elif existing_row.data != serialized_data:
                    existing_row.data = serialized_data

            for key_value, row in existing_rows.items():
                if key_value not in incoming_keys:
                    session.delete(row)

            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def health_check(self) -> dict[str, Any]:
        """健康检查"""
        try:
            session = self.Session()
            try:
                # 尝试执行简单查询
                session.execute(text("SELECT 1"))
                count = session.query(AccountModel).count()
                auth_key_count = session.query(AuthKeyModel).count()
                return {
                    "status": "healthy",
                    "backend": "database",
                    "database_url": self._mask_password(self.database_url),
                    "account_count": count,
                    "auth_key_count": auth_key_count,
                }
            finally:
                session.close()
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": "database",
                "error": str(e),
            }

    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        db_type = "unknown"
        if "sqlite" in self.database_url:
            db_type = "sqlite"
        elif "postgresql" in self.database_url or "postgres" in self.database_url:
            db_type = "postgresql"
        elif "mysql" in self.database_url:
            db_type = "mysql"
        
        return {
            "type": "database",
            "db_type": db_type,
            "description": f"数据库存储 ({db_type})",
            "database_url": self._mask_password(self.database_url),
        }

    @staticmethod
    def _mask_password(url: str) -> str:
        """隐藏数据库连接字符串中的密码"""
        if "://" not in url:
            return url
        try:
            protocol, rest = url.split("://", 1)
            if "@" in rest:
                credentials, host = rest.split("@", 1)
                if ":" in credentials:
                    username, _ = credentials.split(":", 1)
                    return f"{protocol}://{username}:****@{host}"
            return url
        except Exception:
            return url
