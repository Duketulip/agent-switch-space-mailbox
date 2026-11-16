#!/usr/bin/env python3
"""Agent Switch Space CLI —— Agent 用这一个入口完成全部邮箱操作。

    python mailbox_cli.py doctor                     体检（provider / 空间 / 策略）
    python mailbox_cli.py init --agents "WorkBuddy,TRAE"
    python mailbox_cli.py agents
    python mailbox_cli.py send  --from WorkBuddy --to TRAE --type TASK --content "..." 
    python mailbox_cli.py inbox --agent TRAE --max 5
    python mailbox_cli.py claim --agent TRAE --id MSG-20261116-ab12cd34
    python mailbox_cli.py reply --agent TRAE --id MSG-... --text "..."
    python mailbox_cli.py done  --agent TRAE --id MSG-...
    python mailbox_cli.py fail  --agent TRAE --id MSG-... --note "原因"
    python mailbox_cli.py status --agent TRAE

所有命令输出**一行 JSON**，便于 Agent 直接解析。
大文件（>2 GiB）写入/删除需用户确认后加 --confirm-large，否则一律拒绝。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import mailbox_core as core
import policy
from providers import ProviderError, Unsupported, ensure_loaded, create


def _out(obj) -> int:
    print(json.dumps(obj, ensure_ascii=False))
    return 0 if obj.get("ok", True) else 1


def _provider(args):
    ensure_loaded()
    cfg = {}
    if args.provider == "dir_local":
        cfg["root"] = args.ref or os.path.join(os.getcwd(), policy.ROOT_NAME)
    elif args.provider == "quark_drive":
        if args.cli:
            cfg["cli"] = args.cli
        cfg["root_name"] = policy.ROOT_NAME
        cfg["timeout"] = args.timeout
    return create(args.provider, cfg)


def _ledger(args, agent: str) -> core.Ledger:
    path = args.ledger or os.path.expanduser(
        f"~/.agent_switch_space/{args.provider}-{agent}.json")
    return core.Ledger(path, agent)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Agent Switch Space 邮箱 CLI")
    ap.add_argument("--provider", default="quark_drive",
                    choices=["quark_drive", "dir_local"])
    ap.add_argument("--ref", help="dir_local: 根目录（默认 ./AgentSwitchSpace）")
    ap.add_argument("--cli", help="quark_drive: quark-drive.cjs 路径")
    ap.add_argument("--timeout", type=int, default=180, help="单次 CLI 超时（秒）")
    ap.add_argument("--ledger", help="本地台账路径（防重复处理）")
    ap.add_argument("--confirm-large", action="store_true",
                    help="确认执行 >2GiB 的写入/删除（需用户明确同意后才加）")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("policy")
    p.set_defaults(func=lambda a, pr: _out({"ok": True, **policy.describe()}))

    p = sub.add_parser("init")
    p.add_argument("--agents", required=True, help="逗号分隔，如 WorkBuddy,TRAE")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("agents")
    p.set_defaults(func=cmd_agents)

    p = sub.add_parser("send")
    p.add_argument("--from", dest="from_agent", required=True)
    p.add_argument("--to", dest="to_agent", required=True)
    p.add_argument("--type", dest="mtype", default="MESSAGE")
    p.add_argument("--content", required=True)
    p.add_argument("--reply-to", default=None)
    p.add_argument("--task-id", default=None)
    p.add_argument("--no-archive", action="store_true", help="不写入 sent/ 存档（省一次调用）")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("inbox")
    p.add_argument("--agent", required=True)
    p.add_argument("--max", type=int, default=core.MAX_PER_RUN)
    p.add_argument("--full", action="store_true", help="输出完整正文（默认截断预览）")
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("claim")
    p.add_argument("--agent", required=True)
    p.add_argument("--id", dest="mid", required=True)
    p.add_argument("--file", default=None)
    p.set_defaults(func=cmd_claim)

    p = sub.add_parser("reply")
    p.add_argument("--agent", required=True)
    p.add_argument("--id", dest="mid", required=True)
    p.add_argument("--text", required=True)
    p.add_argument("--no-archive", action="store_true")
    p.set_defaults(func=cmd_reply)

    p = sub.add_parser("done")
    p.add_argument("--agent", required=True)
    p.add_argument("--id", dest="mid", required=True)
    p.set_defaults(func=cmd_done)

    p = sub.add_parser("fail")
    p.add_argument("--agent", required=True)
    p.add_argument("--id", dest="mid", required=True)
    p.add_argument("--note", default=None)
    p.set_defaults(func=cmd_fail)

    p = sub.add_parser("status")
    p.add_argument("--agent", required=True)
    p.set_defaults(func=cmd_status)

    args = ap.parse_args(argv)

    try:
        provider = _provider(args)
        return args.func(args, provider)
    except (ProviderError, Unsupported, policy.PolicyError, core.ProtocolError) as e:
        # AlreadyExists / NotFound 都是 ProviderError 的子类，这里一并覆盖
        return _out({"ok": False, "error": type(e).__name__, "message": str(e)})
    except Exception as e:  # 不让 traceback 污染 Agent 的解析
        return _out({"ok": False, "error": type(e).__name__, "message": str(e)})


# ---------------- 子命令 ----------------
def cmd_doctor(args, provider) -> int:
    info = {"ok": True, "provider": provider.name, "policy": policy.describe()}
    try:
        for top in (core.SYSTEM, core.INBOX, core.PROCESSING,
                    core.SENT, core.COMPLETED, core.FAILED, core.ATTACH):
            provider.ensure_dir(core.df(top))
        info["dirs"] = "ok"
    except Exception as e:
        info["ok"] = False
        info["dirs"] = f"{type(e).__name__}: {e}"
    try:
        info["agents"] = core.agents(provider)
    except Exception as e:
        info["agents"] = f"error: {e}"
    return _out(info)


def cmd_init(args, provider) -> int:
    names = [a.strip() for a in args.agents.split(",") if a.strip()]
    return _out({"ok": True, **core.init_space(provider, names,
                                              confirm_large=args.confirm_large)})


def cmd_agents(args, provider) -> int:
    return _out({"ok": True, "space": policy.ROOT_NAME, "agents": core.agents(provider)})


def cmd_send(args, provider) -> int:
    res = core.send(provider, from_agent=args.from_agent, to_agent=args.to_agent,
                    message_type=args.mtype, content=args.content,
                    reply_to=args.reply_to, task_id=args.task_id,
                    archive=not args.no_archive, confirm_large=args.confirm_large)
    return _out(res)


def cmd_inbox(args, provider) -> int:
    core.require_agent(provider, args.agent)
    ledger = _ledger(args, args.agent)
    cands = core.list_inbox(provider, args.agent, args.max)
    items, skipped = [], []
    for entry in cands:
        try:
            msg = core.read_message(provider, args.agent, entry["name"])
            core.validate_for_agent(msg, args.agent)
        except Exception as e:
            skipped.append({"file": entry["name"], "reason": str(e)})
            continue
        if ledger.seen(msg["message_id"]):
            skipped.append({"file": entry["name"], "reason": "already_processed"})
            continue
        body = msg.get("content") or ""
        items.append({
            "message_id": msg["message_id"],
            "from": msg["from_agent"],
            "type": msg["message_type"],
            "created_at": msg.get("created_at"),
            "reply_to": msg.get("reply_to"),
            "task_id": msg.get("task_id"),
            "file": entry["name"],
            "content": body if args.full else (body[:300] + ("…" if len(body) > 300 else "")),
        })
    return _out({"ok": True, "agent": args.agent, "count": len(items),
                 "items": items, "skipped": skipped})


def cmd_claim(args, provider) -> int:
    return _out({"ok": True, **core.claim(provider, args.agent, args.mid, args.file)})


def cmd_reply(args, provider) -> int:
    entry = core.wait_in(provider, args.agent, core.PROCESSING, args.mid)
    if entry is None:
        return _out({"ok": False, "error": "not_claimed",
                     "message": f"{args.mid} 不在 processing 里，请先 claim 再回复"})
    original = core.parse_message(provider.read_text(core.d(args.agent, core.PROCESSING),
                                                     entry["name"]))
    res = core.reply(provider, agent=args.agent, original=original, body=args.text,
                     archive=not args.no_archive, confirm_large=args.confirm_large)
    fin = core.finalize(provider, agent=args.agent, message_id=args.mid, ok=True,
                        confirm_large=args.confirm_large)
    _ledger(args, args.agent).mark(args.mid, "DONE", {"reply_id": res["reply_id"]})
    return _out({"ok": True, **res, "final": fin})


def cmd_done(args, provider) -> int:
    res = core.finalize(provider, agent=args.agent, message_id=args.mid, ok=True,
                        confirm_large=args.confirm_large)
    _ledger(args, args.agent).mark(args.mid, "DONE")
    return _out({"ok": True, **res})


def cmd_fail(args, provider) -> int:
    res = core.finalize(provider, agent=args.agent, message_id=args.mid, ok=False,
                        note=args.note or "未说明原因",
                        confirm_large=args.confirm_large)
    _ledger(args, args.agent).mark(args.mid, "ERROR", {"note": args.note})
    return _out({"ok": True, **res})


def cmd_status(args, provider) -> int:
    return _out({"ok": True, **core.status(provider, args.agent)})


if __name__ == "__main__":
    sys.exit(main())
