from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any


class StorageBackend(ABC):
    """抽象存储后端基类"""

    @abstractmethod
    def load_accounts(self) -> list[dict[str, Any]]:
        """加载所有账号数据"""
        pass

    @abstractmethod
    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存所有账号数据"""
        pass

    def apply_account_changes(
        self,
        upserts: list[dict],
        deleted_tokens: list[str],
        *,
        snapshot: list[dict] | None = None,
    ) -> None:
        """兼容全量后端：优先保存调用方快照，否则按 access_token 合并。

        upsert 是完整账号替换，不是字段 patch。重复 upsert 拒绝，重复删除
        幂等；同键 upsert 胜出。snapshot 是调用方已合并的最终快照。
        """
        serialized, deletions = self._prepare_account_changes(
            upserts, deleted_tokens, snapshot=snapshot,
        )
        if snapshot is None and not serialized and not deletions:
            return

        try:
            if snapshot is None:
                accounts = self.load_accounts()
                # 不静默丢弃损坏或重复的已有记录。
                self._serialize_account_items(accounts)
                merged = {item["access_token"]: item for item in accounts}
                for token in deletions:
                    merged.pop(token, None)
                merged.update((item["access_token"], item) for item in upserts)
                snapshot = list(merged.values())
            self.save_accounts(snapshot)
        except Exception:
            # 后端异常可能包含 JSON、token 或连接凭据，不向调用方透传。
            raise RuntimeError("Failed to apply account changes") from None

    @staticmethod
    def _validate_account_token(token: Any) -> str:
        if not isinstance(token, str) or not token or token.strip() != token:
            raise ValueError(
                "Account access_token must be a non-empty string without surrounding whitespace"
            )
        return token

    @classmethod
    def _serialize_account_items(cls, items: list[dict]) -> dict[str, str]:
        if not isinstance(items, list):
            raise ValueError("Account records must be a list")
        serialized: dict[str, str] = {}
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("Account records must contain dictionaries")
            token = cls._validate_account_token(item.get("access_token"))
            if token in serialized:
                raise ValueError("Duplicate access_token in account changes")
            try:
                serialized[token] = json.dumps(item, ensure_ascii=False, allow_nan=False)
            except Exception:
                # JSON 编码错误（包括非法字典键）也可能包含凭据。
                raise ValueError("Account records must contain valid JSON data") from None
        return serialized

    @classmethod
    def _prepare_account_changes(
        cls,
        upserts: list[dict],
        deleted_tokens: list[str],
        *,
        snapshot: list[dict] | None,
    ) -> tuple[dict[str, str], list[str]]:
        if not isinstance(deleted_tokens, list):
            raise ValueError("Deleted account tokens must be a list")
        if snapshot is not None and not isinstance(snapshot, list):
            raise ValueError("Account snapshot must be a list")
        # 在任何 I/O 之前校验并序列化全部变更。
        serialized = cls._serialize_account_items(upserts)
        deletions = dict.fromkeys(cls._validate_account_token(token) for token in deleted_tokens)
        return serialized, [token for token in deletions if token not in serialized]

    @abstractmethod
    def load_auth_keys(self) -> list[dict[str, Any]]:
        """加载所有鉴权密钥数据"""
        pass

    @abstractmethod
    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存所有鉴权密钥数据"""
        pass

    @abstractmethod
    def health_check(self) -> dict[str, Any]:
        """健康检查，返回存储后端状态"""
        pass

    @abstractmethod
    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        pass
