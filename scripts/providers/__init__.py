#!/usr/bin/env python3
"""Provider 抽象层（文件式邮箱）。

与腾讯文档版 Mailbox 的关键差异：网盘是**目录 + 文件**模型，不是行列模型。
所以逻辑操作是文件操作，而不是 read_rows / write_cells / insert_rows：

    ensure_dir(relpath)            幂等创建目录（含中间层级），返回后端 id
    list_dir(relpath)              列直接子项
    read_text(relpath, name)       下载/读取一个文本文件
    write_text(relpath, name, text) 写入**新**文件（同名视为冲突，不覆盖）
    move(relpath, name, to_relpath) 把文件移到另一个目录（状态变更的唯一手段）
    exists(relpath, name)          是否存在
    stat(relpath, name)            条目元数据（含 id/size/updated_at）
    remove(relpath, name)          可选能力；不支持就抛 ProviderError

Entry = {
    "name": str, "is_dir": bool, "size": int | None,
    "id": str | None, "updated_at": int | None   # 毫秒时间戳
}

约定：
- relpath 用 "/" 分隔且**相对邮箱根**（如 "inbox/WorkBuddy"），不含根目录自身；
- relpath 必须已通过 policy.check_rel 校验，provider 不再重复做安全判断；
- 失败一律抛 ProviderError（或子类），core 捕获后转成可读错误。
"""
from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable


class ProviderError(Exception):
    """Provider 访问失败（网络、权限、参数、产品接口报错等）。"""


class AlreadyExists(ProviderError):
    """目标位置已有同名文件——绝不静默覆盖（网盘同名上传会改名，语义不可控）。"""


class NotFound(ProviderError):
    """目标文件/目录不存在。"""


class Unsupported(ProviderError):
    """该 provider 不支持此操作（如夸克没有删除能力）。"""


@runtime_checkable
class FileProvider(Protocol):
    """文件式邮箱存储后端契约。"""

    name: str

    def ensure_dir(self, relpath: str) -> "str | None":
        ...

    def list_dir(self, relpath: str) -> list[dict[str, Any]]:
        ...

    def read_text(self, relpath: str, name: str) -> str:
        ...

    def write_text(self, relpath: str, name: str, text: str) -> "str | None":
        ...

    def move(self, relpath: str, name: str, to_relpath: str) -> None:
        ...

    def exists(self, relpath: str, name: str) -> bool:
        ...

    def stat(self, relpath: str, name: str) -> "dict[str, Any] | None":
        return None

    def remove(self, relpath: str, name: str) -> None:
        raise Unsupported(f"{getattr(self, 'name', 'provider')} 不支持删除")


_REGISTRY: dict[str, Callable[..., FileProvider]] = {}


def register(name: str):
    def deco(fn):
        _REGISTRY[name] = fn
        return fn
    return deco


def available() -> list[str]:
    return sorted(_REGISTRY)


def create(name: str, config: dict | None = None) -> FileProvider:
    if name not in _REGISTRY:
        raise ProviderError(f"未知 provider '{name}'。可用：{', '.join(available()) or '(无)'}")
    return _REGISTRY[name](config or {})


def ensure_loaded() -> None:
    from . import dir_local, quark_drive  # noqa: F401
