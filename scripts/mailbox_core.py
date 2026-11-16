#!/usr/bin/env python3
"""Agent Switch Space 核心逻辑 —— 与存储产品无关的邮箱协议。

协议要点（受网盘能力倒逼，见 技术可行性调研报告.md）
---------------------------------------------------
1. 一条消息 = 一个 JSON 文件，文件名 = `<message_id>.json`，message_id 全局唯一。
2. **状态 = 目录位置**：inbox → processing → completed/failed。
   JSON 里的 status 只是创建时的初始值；网盘不能原地改写，所以不靠它表达状态。
3. 写入即新建。同名冲突一律视为错误，绝不覆盖（网盘会静默改名）。
4. 每次写入都**内容回读比对**；读不回来或内容不符 → 判定写入失败。
5. 不删除任何消息。失败的进 failed/，权限交给用户，不做清理。
6. 一轮最多 5 条，超出留到下一轮。

安全策略（policy.py）在这里整体生效：
  R1 只在 AgentSwitchSpace 下工作；
  R2 写入/删除 >2GiB 必须先确认。
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import datetime, timedelta, timezone

import policy
from providers import AlreadyExists, NotFound, ProviderError

PROTOCOL_VERSION = "0.1"
MAX_PER_RUN = 5
MAX_CONTENT_BYTES = 64 * 1024
MAX_FILENAME_BYTES = 200

T_MSG = "MESSAGE"
T_PING = "PING"
T_TASK = "TASK"
T_REPLY = "REPLY"
T_NOTICE = "NOTICE"
MESSAGE_TYPES = (T_PING, T_MSG, T_TASK, T_REPLY, T_NOTICE)
REPLY_REQUIRED = (T_PING, T_MSG, T_TASK)   # 需要回复的类型
REPLY_FREE = (T_REPLY, T_NOTICE)           # 只标记完成，不回复

INBOX, PROCESSING, SENT, COMPLETED, FAILED = (
    "inbox", "processing", "sent", "completed", "failed")
SYSTEM, ATTACH = "system", "attachments"

TMP_TAG = ".part"


class ProtocolError(Exception):
    """消息本身不合协议（缺字段、类型非法、超长等）。"""


class ReadbackMismatch(ProtocolError):
    """写入后回读内容不一致 —— 视为写入失败，绝不宣称成功。"""


# ---------------- 基础工具 ----------------
def now_iso() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def new_message_id() -> str:
    d = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    return f"MSG-{d}-{uuid.uuid4().hex[:8]}"


def d(agent: str, kind: str) -> str:
    """拼出白名单内的相对路径，例如 d('WorkBuddy','inbox') -> 'inbox/WorkBuddy'。"""
    path = f"{kind}/{agent}"
    return policy.check_rel(path)


def df(kind: str) -> str:
    return policy.check_rel(kind)


def _name_for(message_id: str) -> str:
    name = f"{message_id}.json"
    if len(name.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise ProtocolError(f"消息文件名过长：{name}")
    return name


# ---------------- 写入 + 回读 ----------------
def write_verified(provider, relpath: str, name: str, text: str,
                   *, confirm_large: bool = False) -> str:
    """写入一个**新**文件并回读校验。任何一步失败都抛异常，绝不返回成功。"""
    policy.check_rel(relpath)
    raw = text.encode("utf-8")
    policy.guard_size("write", len(raw), confirm_large)   # R2

    if provider.exists(relpath, name):
        raise AlreadyExists(f"目标已存在，拒绝覆盖：{relpath}/{name}")

    provider.write_text(relpath, name, text)

    try:
        got = provider.read_text(relpath, name)
    except NotFound:
        # 网盘同名上传会静默改名 → 读不回原名字，说明发生了冲突改名
        raise AlreadyExists(f"写入后被改名（同名冲突），请改用唯一文件名：{relpath}/{name}")
    if got != text:
        raise ReadbackMismatch(
            f"回读内容不一致：{relpath}/{name}"
            f"（本地 {len(raw)}B / 远端 {len(got.encode('utf-8'))}B）")
    return name


# ---------------- 初始化 ----------------
def init_space(provider, agents: list[str], *, confirm_large: bool = False) -> dict:
    """建立 AgentSwitchSpace 骨架。幂等：已存在的不重建、不覆盖。"""
    created = []
    for top in (SYSTEM, ATTACH, INBOX, PROCESSING, SENT, COMPLETED, FAILED):
        provider.ensure_dir(df(top))
        created.append(top)
    for kind in (INBOX, PROCESSING, SENT, COMPLETED, FAILED):
        for a in agents:
            provider.ensure_dir(d(a, kind))
    provider.ensure_dir(policy.check_rel(f"{SYSTEM}/agents"))

    cfg_name = "mailbox_config.json"
    cfg_rel = df(SYSTEM)
    if not provider.exists(cfg_rel, cfg_name):
        cfg = {
            "protocol_version": PROTOCOL_VERSION,
            "space": policy.ROOT_NAME,
            "max_per_run": MAX_PER_RUN,
            "max_content_bytes": MAX_CONTENT_BYTES,
            "state_model": "directory",
            "created_at": now_iso(),
        }
        write_verified(provider, cfg_rel, cfg_name,
                       json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                       confirm_large=confirm_large)

    reg = register_agents(provider, agents, confirm_large=confirm_large)
    return {"space": policy.ROOT_NAME, "dirs": created,
            "agents": reg["registered"], "skipped": reg["skipped"]}


def register_agents(provider, agents: list[str], *, confirm_large: bool = False) -> dict:
    """注册 Agent：system/agents/<name>.json，一个 Agent 一个文件（只增不改）。"""
    if not agents:
        raise ProtocolError("agents 不能为空")
    rel = policy.check_rel(f"{SYSTEM}/agents")
    provider.ensure_dir(rel)
    registered, skipped = [], []
    for a in agents:
        if not a or "/" in a or "\\" in a:
            raise ProtocolError(f"Agent 名非法：{a!r}")
        name = f"{a}.json"
        if provider.exists(rel, name):
            skipped.append(a)
            continue
        rec = {"name": a, "protocol_version": PROTOCOL_VERSION, "created_at": now_iso()}
        write_verified(provider, rel, name,
                       json.dumps(rec, ensure_ascii=False, indent=2) + "\n",
                       confirm_large=confirm_large)
        registered.append(a)
    return {"registered": registered, "skipped": skipped}


def agents(provider) -> list[str]:
    rel = policy.check_rel(f"{SYSTEM}/agents")
    try:
        items = provider.list_dir(rel)
    except (NotFound, ProviderError):
        return []
    return sorted(i["name"][:-5] for i in items
                  if not i["is_dir"] and i["name"].endswith(".json"))


def require_agent(provider, name: str) -> None:
    known = agents(provider)
    if not known:
        raise ProtocolError("邮箱里还没有注册任何 Agent，请先执行 init")
    if name not in known:
        raise ProtocolError(f"未注册的 Agent：{name}（已注册：{', '.join(known)}）")


# ---------------- 消息构造与校验 --------------
def build_message(*, from_agent: str, to_agent: str, message_type: str, content: str,
                  reply_to: "str | None" = None, task_id: "str | None" = None,
                  attachments: "list | None" = None,
                  metadata: "dict | None" = None,
                  message_id: "str | None" = None) -> dict:
    mtype = (message_type or "").upper()
    if mtype not in MESSAGE_TYPES:
        raise ProtocolError(f"未知消息类型：{message_type}（允许：{', '.join(MESSAGE_TYPES)}）")
    if not from_agent or not to_agent:
        raise ProtocolError("from_agent / to_agent 不能为空")
    if from_agent == to_agent:
        raise ProtocolError("不支持自己给自己发消息（V0.1 单接收者）")
    body = content if isinstance(content, str) else str(content)
    if len(body.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ProtocolError(
            f"消息正文超过上限 {MAX_CONTENT_BYTES} B（当前 {len(body.encode('utf-8'))} B）")
    ts = now_iso()
    return {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": message_id or new_message_id(),
        "from_agent": from_agent,
        "to_agent": to_agent,
        "message_type": mtype,
        "content": body,
        "status": "NEW",
        "created_at": ts,
        "updated_at": ts,
        "reply_to": reply_to,
        "task_id": task_id,
        "attachments": list(attachments or []),
        "metadata": dict(metadata or {}),
    }


REQUIRED_FIELDS = ("protocol_version", "message_id", "from_agent", "to_agent",
                   "message_type", "content", "status", "created_at")


def parse_message(text: str) -> dict:
    try:
        msg = json.loads(text)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"消息不是合法 JSON：{e}")
    if not isinstance(msg, dict):
        raise ProtocolError("消息顶层必须是对象")
    missing = [k for k in REQUIRED_FIELDS if k not in msg]
    if missing:
        raise ProtocolError(f"消息缺字段：{', '.join(missing)}")
    if (msg.get("message_type") or "").upper() not in MESSAGE_TYPES:
        raise ProtocolError(f"未知消息类型：{msg.get('message_type')!r}")
    if len(str(msg.get("content") or "").encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ProtocolError("消息正文超过上限")
    return msg


# ---------------- 发送 ----------------
def send(provider, *, from_agent: str, to_agent: str, message_type: str, content: str,
         reply_to: "str | None" = None, task_id: "str | None" = None,
         attachments: "list | None" = None, metadata: "dict | None" = None,
         archive: bool = True, confirm_large: bool = False) -> dict:
    """发一条消息到接收方 inbox。送达 + 回读校验成功才算成功。"""
    require_agent(provider, from_agent)
    require_agent(provider, to_agent)
    msg = build_message(from_agent=from_agent, to_agent=to_agent,
                        message_type=message_type, content=content,
                        reply_to=reply_to, task_id=task_id,
                        attachments=attachments, metadata=metadata)
    name = _name_for(msg["message_id"])
    text = json.dumps(msg, ensure_ascii=False, indent=2) + "\n"

    write_verified(provider, d(to_agent, INBOX), name, text, confirm_large=confirm_large)

    archived = False
    if archive:
        try:
            write_verified(provider, d(from_agent, SENT), name, text, confirm_large=confirm_large)
            archived = True
        except (AlreadyExists, ProviderError) as e:
            # 送达已成功；存档失败只降级告警，不谎报整体失败
            msg.setdefault("metadata", {})["archive_error"] = str(e)
    return {"ok": True, "message_id": msg["message_id"], "to": to_agent,
            "type": msg["message_type"], "bytes": len(text.encode("utf-8")),
            "verified": True, "archived": archived}


# ---------------- 领取与读取 -------------
def list_inbox(provider, agent: str, max_n: int = MAX_PER_RUN) -> list[dict]:
    """列出 inbox 里最老的 N 个候选（只读，不改状态）。"""
    policy.check_rel(d(agent, INBOX))
    try:
        items = provider.list_dir(d(agent, INBOX))
    except NotFound:
        return []
    files = [i for i in items if not i["is_dir"] and i["name"].endswith(".json")]
    files.sort(key=lambda i: i["name"])          # message_id 内含日期，升序≈FIFO
    return files[:max_n]


def read_message(provider, agent: str, name: str) -> dict:
    text = provider.read_text(d(agent, INBOX), name)
    msg = parse_message(text)
    msg["_file"] = name
    msg["_dir"] = INBOX
    return msg


def validate_for_agent(msg: dict, agent: str) -> None:
    if msg.get("to_agent") != agent:
        raise ProtocolError(f"to_agent 不是本 Agent（{msg.get('to_agent')} ≠ {agent}）")
    if msg.get("from_agent") == agent:
        raise ProtocolError("自己发给自己的消息，跳过")
    if (msg.get("status") or "").upper() not in ("NEW", "PENDING"):
        raise ProtocolError(f"消息状态不是 NEW/PENDING（{msg.get('status')}）")


def claim(provider, agent: str, message_id: str, file_name: "str | None" = None) -> dict:
    """把消息从 inbox 移到 processing —— 这是唯一的"领取"动作。

    移动后按 fid 校验；若源目录已无该文件且目标目录已有，即视为领取成功。
    """
    name = file_name or _name_for(message_id)
    entry = next((i for i in provider.list_dir(d(agent, INBOX)) if i["name"] == name), None)
    if entry is None:
        raise NotFound(f"inbox 里没有这条消息：{name}")
    provider.move(d(agent, INBOX), name, d(agent, PROCESSING))
    return {"claimed": message_id, "to": PROCESSING}


def find_in(provider, agent: str, kind: str, message_id: str) -> "dict | None":
    try:
        items = provider.list_dir(d(agent, kind))
    except NotFound:
        return None
    for i in items:
        if i["is_dir"]:
            continue
        # 撞名可能被改名成 xxx(1).json，用前缀匹配兜住
        if i["name"] == f"{message_id}.json" or i["name"].startswith(f"{message_id}"):
            return i
    return None


def wait_in(provider, agent: str, kind: str, message_id: str,
            tries: int = 3, delay: float = 1.5) -> "dict | None":
    """等目录里出现这条消息（网盘的移动有数秒可见性延迟）。"""
    for i in range(max(1, tries)):
        hit = find_in(provider, agent, kind, message_id)
        if hit is not None:
            return hit
        if i < tries - 1:
            time.sleep(delay)
    return None


# ---------------- 回复 ----------------
def reply(provider, *, agent: str, original: dict, body: str,
          archive: bool = True, confirm_large: bool = False) -> dict:
    """对原消息回一条 REPLY，并写入原发送者的 inbox。"""
    orig_id = original["message_id"]
    to_agent = original["from_agent"]
    msg = build_message(from_agent=agent, to_agent=to_agent, message_type=T_REPLY,
                        content=body, reply_to=orig_id,
                        task_id=original.get("task_id"),
                        metadata={"original_status": "DONE",
                                  "original_type": original.get("message_type")})
    name = _name_for(msg["message_id"])
    text = json.dumps(msg, ensure_ascii=False, indent=2) + "\n"

    write_verified(provider, d(to_agent, INBOX), name, text, confirm_large=confirm_large)
    archived = False
    if archive:
        try:
            write_verified(provider, d(agent, SENT), name, text, confirm_large=confirm_large)
            archived = True
        except (AlreadyExists, ProviderError):
            pass
    return {"ok": True, "reply_id": msg["message_id"], "to": to_agent,
            "verified": True, "archived": archived}


# ---------------- 收尾 ----------------
def finalize(provider, *, agent: str, message_id: str, ok: bool,
             note: "str | None" = None, confirm_large: bool = False) -> dict:
    """把 processing 里的消息移到 completed / failed。失败原因写一份 .error.json。"""
    target = COMPLETED if ok else FAILED
    entry = wait_in(provider, agent, PROCESSING, message_id)
    if entry is None:
        raise NotFound(f"processing 里没有这条消息：{message_id}（可能已被处理）")

    if not ok and note:
        diag = {"message_id": message_id, "agent": agent, "at": now_iso(),
                "error": note}
        try:
            write_verified(provider, d(agent, FAILED),
                           f"{message_id}.error.json",
                           json.dumps(diag, ensure_ascii=False, indent=2) + "\n",
                           confirm_large=confirm_large)
        except (AlreadyExists, ProviderError):
            pass   # 诊断文件失败不影响主流程

    provider.move(d(agent, PROCESSING), entry["name"], d(agent, target))
    landed = wait_in(provider, agent, target, message_id)
    return {"message_id": message_id, "state": "DONE" if ok else "ERROR",
            "dir": target, "verified": landed is not None}


# ---------------- 概览 ----------------
def status(provider, agent: str) -> dict:
    out = {"agent": agent, "space": policy.ROOT_NAME}
    for kind in (INBOX, PROCESSING, SENT, COMPLETED, FAILED):
        try:
            items = provider.list_dir(d(agent, kind))
            out[kind] = sum(1 for i in items if not i["is_dir"])
        except NotFound:
            out[kind] = 0
        except ProviderError as e:
            out[kind] = f"error: {e}"
    return out


class Ledger:
    """本地处理台账：记住已经处理过的 message_id，防止重复处理。"""

    def __init__(self, path: str, agent: str):
        self.path = path
        self.agent = agent
        self.data = {"agent": agent, "processed": {}}
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded.get("processed"), dict):
                    self.data = loaded
            except (json.JSONDecodeError, OSError):
                pass

    def seen(self, message_id: str) -> bool:
        return message_id in self.data["processed"]

    def mark(self, message_id: str, state: str, extra: "dict | None" = None) -> None:
        rec = {"state": state, "at": now_iso()}
        if extra:
            rec.update(extra)
        self.data["processed"][message_id] = rec
        self.save()

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)
