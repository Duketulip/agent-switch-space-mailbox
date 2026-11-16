#!/usr/bin/env python3
"""本地目录 provider —— 离线自测 / 本地 JSON 邮箱。

root 指向一个本地目录（默认 <cwd>/AgentSwitchSpace）。所有操作被夹在 root 内，
即使上游校验被绕过，这里还会用 realpath 再做一次包含性检查。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time

from . import AlreadyExists, NotFound, ProviderError, register

_FNAME_RE = re.compile(r'^[^<>:"|?*\\/]+$')


@register("dir_local")
def _make(cfg: dict) -> "DirLocalProvider":
    return DirLocalProvider(cfg)


class DirLocalProvider:
    name = "dir_local"

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.root = os.path.realpath(cfg.get("root") or os.path.join(os.getcwd(), "AgentSwitchSpace"))
        self.tmp = os.path.realpath(cfg.get("tmp") or os.path.join(tempfile.gettempdir(), "agsstmp"))

    # ---- 内部 ----
    def _path(self, relpath: str) -> str:
        parts = [p for p in str(relpath).replace("\\", "/").split("/") if p]
        p = os.path.realpath(os.path.join(self.root, *parts)) if parts else self.root
        if p != self.root and not p.startswith(self.root + os.sep):
            raise ProviderError(f"路径逃出邮箱根目录：{relpath!r}")
        return p

    def ensure_dir(self, relpath: str) -> "str | None":
        p = self._path(relpath)
        os.makedirs(p, exist_ok=True)
        return p

    def list_dir(self, relpath: str) -> list[dict]:
        p = self._path(relpath)
        if not os.path.isdir(p):
            raise NotFound(f"目录不存在：{relpath!r}")
        out = []
        for name in sorted(os.listdir(p)):
            fp = os.path.join(p, name)
            st = os.stat(fp)
            out.append({
                "name": name,
                "is_dir": os.path.isdir(fp),
                "size": None if os.path.isdir(fp) else st.st_size,
                "id": fp,
                "updated_at": int(st.st_mtime * 1000),
            })
        return out

    def _file(self, relpath: str, name: str) -> str:
        if not _FNAME_RE.match(name or ""):
            raise ProviderError(f"非法文件名：{name!r}")
        return os.path.join(self._path(relpath), name)

    def read_text(self, relpath: str, name: str) -> str:
        fp = self._file(relpath, name)
        if not os.path.isfile(fp):
            raise NotFound(f"文件不存在：{relpath}/{name}")
        with open(fp, "rb") as fh:
            return fh.read().decode("utf-8-sig")

    def write_text(self, relpath: str, name: str, text: str) -> "str | None":
        fp = self._file(relpath, name)
        if os.path.exists(fp):
            raise AlreadyExists(f"目标已存在，拒绝覆盖：{relpath}/{name}")
        os.makedirs(os.path.dirname(fp), exist_ok=True)
        # 先写临时文件再 os.link 占位，尽量贴近"新建即成功/已存在即失败"的语义
        tmp = fp + f".tmp{os.getpid()}"
        with open(tmp, "wb") as fh:
            fh.write(text.encode("utf-8"))
        try:
            os.link(tmp, fp)          # 原子：同名已存在会抛 FileExistsError
        except FileExistsError:
            raise AlreadyExists(f"目标已存在，拒绝覆盖：{relpath}/{name}")
        except OSError:
            if os.path.exists(fp):
                raise AlreadyExists(f"目标已存在，拒绝覆盖：{relpath}/{name}")
            os.replace(tmp, fp)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return fp

    def move(self, relpath: str, name: str, to_relpath: str) -> None:
        src = self._file(relpath, name)
        if not os.path.isfile(src):
            raise NotFound(f"文件不存在：{relpath}/{name}")
        dst_dir = self.ensure_dir(to_relpath)
        dst = os.path.join(dst_dir, name)
        if os.path.exists(dst):
            # 与网盘行为保持一致：撞名不删数据，改名加后缀
            stem, ext = os.path.splitext(name)
            i = 1
            while os.path.exists(os.path.join(dst_dir, f"{stem}({i}){ext}")):
                i += 1
            dst = os.path.join(dst_dir, f"{stem}({i}){ext}")
        os.replace(src, dst)

    def exists(self, relpath: str, name: str) -> bool:
        try:
            return os.path.exists(self._file(relpath, name))
        except ProviderError:
            return False

    def stat(self, relpath: str, name: str) -> "dict | None":
        fp = self._file(relpath, name)
        if not os.path.exists(fp):
            return None
        st = os.stat(fp)
        return {"name": name, "is_dir": os.path.isdir(fp),
                "size": None if os.path.isdir(fp) else st.st_size,
                "id": fp, "updated_at": int(st.st_mtime * 1000)}

    def remove(self, relpath: str, name: str) -> None:
        fp = self._file(relpath, name)
        if not os.path.exists(fp):
            raise NotFound(f"文件不存在：{relpath}/{name}")
        if os.path.isdir(fp):
            shutil.rmtree(fp)
        else:
            os.unlink(fp)
