#!/usr/bin/env python3
"""Quark provider —— 走官方 CLI（quarkclouddrive）的 quark-drive.cjs。

实测事实（见 技术可行性调研报告.md）：
- 同名上传不覆盖，会自动改名成 name(1).json → 所以 write_text 前先 exists，
  冲突就抛 AlreadyExists，绝不"写了再说"；
- 移动撞名也会改名 → 移动后按 fid 校验，不按文件名校验；
- 没有删除命令 → remove 抛 Unsupported；
- 每次调用 6–8 秒 → fid 缓存 + 尽量少的往返；
- browse 返回的 fid 可能带 "|" 尾串，可原样回传；某些接口需要裸 fid，故带 fallback。

依赖：node + 官方 skill 包 + 该环境已完成一次 login。
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time

from . import AlreadyExists, NotFound, ProviderError, Unsupported, register

DEFAULT_CLI_HINT = os.path.join("_vendor", "quark", "scripts", "quark-drive.cjs")


@register("quark_drive")
def _make(cfg: dict) -> "QuarkDriveProvider":
    return QuarkDriveProvider(cfg)


def _new_session_id() -> str:
    return f"{int(time.time())}-{''.join(random.choices('0123456789abcdefghijklmnopqrstuvwxyz', k=6))}"


def _bare_fid(fid: str) -> str:
    """去掉 browse 返回的 `|指纹` 尾串（部分接口只接受裸 fid）。"""
    return fid.split("|")[0] if "|" in fid else fid


class QuarkDriveProvider:
    name = "quark_drive"

    def __init__(self, cfg: dict | None = None):
        cfg = cfg or {}
        self.cli = cfg.get("cli") or self._guess_cli()
        self.node = cfg.get("node") or "node"
        self.root_name = cfg.get("root_name") or "AgentSwitchSpace"
        self.session_input = cfg.get("session_input") or "agent switch space mailbox"
        self.session_id = cfg.get("session_id") or _new_session_id()
        self.timeout = int(cfg.get("timeout") or 180)
        self.verify_retries = int(cfg.get("verify_retries") or 6)
        self.verify_delay = float(cfg.get("verify_delay") or 2.0)
        self.tmp = os.path.realpath(cfg.get("tmp") or os.path.join(tempfile.gettempdir(), "agsstmp"))
        os.makedirs(self.tmp, exist_ok=True)
        self._fid_cache: dict[str, str] = {}
        self._base_fid: "str | None" = None

    @staticmethod
    def _guess_cli() -> str:
        env = os.environ.get("QUARK_DRIVE_CLI")
        if env:
            return env
        here = os.path.dirname(os.path.abspath(__file__))     # <proj>/scripts/providers
        cand = os.path.realpath(os.path.join(here, "..", "..", DEFAULT_CLI_HINT))
        if os.path.isfile(cand):
            return cand
        raise ProviderError(
            "找不到 quark-drive.cjs。请设置环境变量 QUARK_DRIVE_CLI 或 config.cli 指向它"
        )

    # ---------------- 调用底层 ----------------
    def _run(self, args: list[str]) -> tuple[dict, list[dict]]:
        cmd = [self.node, self.cli,
               "--session-input", self.session_input, "--session-id", self.session_id] + args
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=self.timeout)
        lines = []
        for ln in p.stdout.splitlines():
            ln = ln.strip()
            if ln.startswith("{"):
                try:
                    lines.append(json.loads(ln))
                except json.JSONDecodeError:
                    pass
        result = next((x for x in reversed(lines) if x.get("type") == "result"), None)
        if result is None:
            raise ProviderError(f"{args[0]} 无结果输出（exit={p.returncode}）：{(p.stdout or p.stderr)[-200:]}")
        code = result.get("code")
        if p.returncode != 0 or code not in (0, None):
            if code in (-103, -1408) or "未授权" in str(result.get("msg")):
                raise ProviderError(
                    f"夸克网盘未授权或授权已过期（{code}: {result.get('msg')}）。"
                    f"请先执行一次 login 完成授权，再重试。"
                )
            raise ProviderError(f"{args[0]} 失败（{code}: {result.get('msg')}）")
        return result, lines

    def _browse(self, fid: str) -> list[dict]:
        result, lines = self._run(["browse", "--parent-fid", fid, "--all"])
        items = [x["data"] for x in lines if x.get("type") == "list" and isinstance(x.get("data"), dict)]
        art = next((x for x in lines if x.get("type") == "artifact"), None)
        if art and art.get("data", {}).get("file_path"):
            path = art["data"]["file_path"]
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    for ln in fh.read().decode("utf-8-sig").splitlines():
                        if ln.strip():
                            try:
                                items.append(json.loads(ln))
                            except json.JSONDecodeError:
                                pass
        return items

    def _create_folder(self, name: str, parent_fid: str) -> str:
        for fid in (parent_fid, _bare_fid(parent_fid)):
            try:
                result, _ = self._run(["create-folder", "--dir-path", name, "--parent-fid", fid])
                return result["data"]["fid"]
            except ProviderError:
                continue
        raise ProviderError(f"创建目录失败：{name}（parent={parent_fid}）")

    def _root_fid(self) -> str:
        if self._base_fid:
            return self._base_fid
        for it in self._browse("0"):
            if it.get("filename") == self.root_name and (it.get("file_type") == "0"
                                                         or it.get("category") == 0):
                self._base_fid = it["fid"]
                self._fid_cache[""] = it["fid"]
                return it["fid"]
        self._base_fid = self._create_folder(self.root_name, "0")
        self._fid_cache[""] = self._base_fid
        return self._base_fid

    def _fid(self, relpath: str, create: bool = False) -> str:
        rel = "/".join([p for p in str(relpath).replace("\\", "/").split("/") if p])
        if rel in self._fid_cache:
            return self._fid_cache[rel]
        parent = self._root_fid()
        parts = rel.split("/") if rel else []
        walked = ""
        for part in parts:
            walked = f"{walked}/{part}" if walked else part
            if walked in self._fid_cache:
                parent = self._fid_cache[walked]
                continue
            hit = next((it for it in self._browse(parent) if it.get("filename") == part), None)
            if hit:
                parent = hit["fid"]
            elif create:
                parent = self._create_folder(part, parent)
            else:
                raise NotFound(f"目录不存在：{walked}")
            self._fid_cache[walked] = parent
        return parent

    # ---------------- 契约实现 ----------------
    def ensure_dir(self, relpath: str) -> "str | None":
        return self._fid(relpath, create=True)

    def list_dir(self, relpath: str) -> list[dict]:
        out = []
        for it in self._browse(self._fid(relpath)):
            is_dir = (it.get("file_type") == "0" or it.get("category") == 0)
            out.append({
                "name": it.get("filename"),
                "is_dir": is_dir,
                "size": None if is_dir else it.get("size"),
                "id": it.get("fid"),
                "updated_at": it.get("updated_at"),
            })
        return out

    def _entry(self, relpath: str, name: str) -> "dict | None":
        return next((e for e in self.list_dir(relpath) if e["name"] == name), None)

    def exists(self, relpath: str, name: str) -> bool:
        return self._entry(relpath, name) is not None

    def stat(self, relpath: str, name: str) -> "dict | None":
        return self._entry(relpath, name)

    def read_text(self, relpath: str, name: str) -> str:
        entry = self._entry(relpath, name)
        if entry is None:
            raise NotFound(f"文件不存在：{relpath}/{name}")
        work = os.path.join(self.tmp, f"dl-{int(time.time() * 1000)}")
        os.makedirs(work, exist_ok=True)
        try:
            self._run(["download", "--fid", entry["id"], "--output-dir", work, "--overwrite"])
            got = None
            for root_, _, files in os.walk(work):
                for f in files:
                    if f == name:
                        got = os.path.join(root_, f)
                        break
                if got:
                    break
            if got is None:
                raise ProviderError(f"下载后未找到文件：{name}")
            with open(got, "rb") as fh:
                return fh.read().decode("utf-8-sig")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def write_text(self, relpath: str, name: str, text: str) -> "str | None":
        """只负责"上传这个新文件"，不做存在性预检。

        为什么：网盘同名上传会静默改名成 name(1).json，预检反而多花 7 秒。
        冲突检测交给 core 的**内容回读**：读不回来（被改名）或读回来内容不符
        （覆盖失败/旧文件）都会被 core 判为 AlreadyExists，绝不静默通过。
        """
        parent = self._fid(relpath, create=True)
        work = os.path.join(self.tmp, f"up-{int(time.time() * 1000)}")
        os.makedirs(work, exist_ok=True)
        local = os.path.join(work, name)
        try:
            with open(local, "wb") as fh:
                fh.write(text.encode("utf-8"))
            result, _ = self._run(["upload", local, "--parent-fid", parent])
            fids = result.get("data", {}).get("fids") or []
            if not fids:
                raise ProviderError(f"上传未返回 fid：{result.get('msg')}")
            return fids[0]
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def move(self, relpath: str, name: str, to_relpath: str) -> None:
        entry = self._entry(relpath, name)
        if entry is None:
            raise NotFound(f"文件不存在：{relpath}/{name}")
        target = self._fid(to_relpath, create=True)
        for fid in (entry["id"], _bare_fid(entry["id"])):
            try:
                self._run(["move", fid, "--target-fid", target])
                break
            except ProviderError:
                continue
        else:
            raise ProviderError(f"移动失败：{relpath}/{name} → {to_relpath}")

        # 实测：move 之后既不能只信 fid（fid 会变），也不能只信一次列表（可见性有数秒延迟）。
        # 判定成功的条件**只有正向证据**：目标目录里出现同名条目（含撞名改名 `stem(1).ext`）
        # 或 fid 命中。要求 move 命令本身返回成功 + 目标目录确认。
        #
        # 刻意**不**接受"原目录已消失"作为成功依据（外部评审指出）：源文件消失可能是被
        # 服务端清理、被第三方移走，甚至是列表抖动，用它判定成功会产生假阳性。宁可失败
        # 上报，也不静默当成已完成。
        stem, ext = os.path.splitext(name)
        pat = re.compile(rf"^{re.escape(stem)}(\(\d+\))?{re.escape(ext)}$")
        want = _bare_fid(entry["id"])

        for attempt in range(self.verify_retries):
            for e in self.list_dir(to_relpath):
                if e["is_dir"]:
                    continue
                if pat.match(e["name"] or "") or _bare_fid(e["id"] or "") == want:
                    return
            if attempt < self.verify_retries - 1:
                time.sleep(self.verify_delay)
        raise ProviderError(
            f"移动校验失败：{relpath}/{name} → {to_relpath}"
            f"（{self.verify_retries} 次重试后目标目录仍未出现该文件；"
            f"源目录状态不作为成功依据）")

    def remove(self, relpath: str, name: str) -> None:
        raise Unsupported("夸克网盘 CLI 没有删除能力；邮箱只允许移动归档，不允许删除")
