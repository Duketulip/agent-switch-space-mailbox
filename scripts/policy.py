#!/usr/bin/env python3
"""安全策略层 —— 凌驾于 provider 之上、core 之下的硬约束。

两条不可绕过的规则（用户明确要求）：
  R1 只在 AgentSwitchSpace 目录下工作，其他文件一律不允许访问。
  R2 写入或删除超过 2 GiB 的文件，必须先得到用户确认。

设计要点
--------
- 所有路径以**相对邮箱根**的形式进入本层，逐段校验后才交给 provider。
- 校验在这里做一次，core 与 provider 都不再各自判断；provider 只接受
  "已经通过校验" 的相对路径，所以任何 provider 都绕不过去。
- 大文件判定用**真实字节数**：写入前看本地文件大小，删除前看远端条目 size。
- 未确认则抛 ConfirmationRequired，由 CLI 转成需要用户确认的明确提示，
  **绝不"先写了再说"**。
"""
from __future__ import annotations

#: 唯一允许的工作根目录名（R1）。不要做成可配置项，否则规则就形同虚设。
ROOT_NAME = "AgentSwitchSpace"

#: 邮箱根下允许存在的顶层目录（白名单）。root 自身是空 relpath。
ALLOWED_TOP = frozenset({
    "system", "inbox", "processing", "sent", "completed", "failed", "attachments",
})

#: 单文件大小硬上限（R2）：2 GiB
LARGE_FILE_BYTES = 2 * 1024 ** 3

#: Windows 保留字符 —— 网盘虽不做文件名校验，但本地侧会炸，统一挡掉
_BAD_CHARS = set('<>:"|?*')


class PolicyError(Exception):
    """违反安全策略（路径越界、非法名称等）。"""


class ConfirmationRequired(PolicyError):
    """需要用户确认才能继续（当前只有 >2GiB 的写入/删除）。"""

    def __init__(self, action: str, size: int, limit: int):
        self.action, self.size, self.limit = action, size, limit
        super().__init__(
            f"{action} 目标文件 {human(size)} 超过 {human(limit)}，"
            f"必须先获得用户明确确认后再执行（加 --confirm-large 重试）"
        )


def human(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n} B"


def normalize_rel(rel: str) -> list[str]:
    """把相对路径切成段，并挡掉一切越界写法（R1 的第一道闸）。"""
    if rel is None:
        raise PolicyError("路径不能为空")
    s = str(rel).strip().replace("\\", "/")
    if s in ("", "/", "."):
        return []
    if s.startswith("/"):
        raise PolicyError(f"禁止绝对路径：{rel!r}")
    if len(s) > 1 and s[1] == ":":
        raise PolicyError(f"禁止盘符路径：{rel!r}")
    parts = [p for p in s.split("/")]
    for p in parts:
        if p in ("", ".", ".."):
            raise PolicyError(f"路径含非法段 {p!r}：{rel!r}")
        if any(c in _BAD_CHARS for c in p):
            raise PolicyError(f"路径含非法字符：{p!r}")
        if p.strip() != p:
            raise PolicyError(f"路径段首尾有空白：{p!r}")
        if p in (ROOT_NAME,):
            raise PolicyError(f"相对路径不应包含根目录名 {ROOT_NAME}：{rel!r}")
    return parts


def check_rel(rel: str) -> str:
    """校验并返回规范化相对路径（R1）。"""
    parts = normalize_rel(rel)
    if parts and parts[0] not in ALLOWED_TOP:
        raise PolicyError(
            f"只允许在 {ROOT_NAME} 的白名单目录下工作，拒绝 {parts[0]!r}；"
            f"允许：{', '.join(sorted(ALLOWED_TOP))}"
        )
    return "/".join(parts)


def guard_size(action: str, size: "int | None", confirmed: bool) -> None:
    """R2：写入/删除超过 2 GiB 必须有确认。

    size 未知（None）时按"不允许"处理——宁可停下来问，也不猜。
    """
    if action not in ("write", "delete"):
        return
    if size is None:
        raise PolicyError(f"{action} 无法确定文件大小，已拒绝执行（请先取到真实字节数）")
    if int(size) > LARGE_FILE_BYTES and not confirmed:
        raise ConfirmationRequired(action, int(size), LARGE_FILE_BYTES)


def describe() -> dict:
    """给 doctor / SKILL.md 用的一份策略快照。"""
    return {
        "root_name": ROOT_NAME,
        "allowed_top": sorted(ALLOWED_TOP),
        "large_file_limit_bytes": LARGE_FILE_BYTES,
        "large_file_limit_human": human(LARGE_FILE_BYTES),
    }
