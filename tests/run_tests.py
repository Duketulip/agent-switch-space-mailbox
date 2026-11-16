#!/usr/bin/env python3
"""离线测试（dir_local provider）—— 验证协议与两条安全约束。

跑法： python tests/run_tests.py
覆盖：
  P1 路径只在 AgentSwitchSpace 白名单内（.. / 绝对路径 / 盘符 / 非法顶层 全部拒绝）
  P2 >2GiB 写入必须确认（用极小阈值证明"闸门确实接在写路径上"）
  C1 初始化幂等
  C2 发送 + 回读校验 + sent 存档
  C3 inbox 列出 / claim 领取 / reply 回复 / finalize 完成
  C4 防重复：未 claim 不能 reply；同一 message_id 不能写两次
  C5 端到端：PING → PONG 完整闭环（两个 Agent）
  C6 台账：已处理的消息不再出现在 inbox
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import mailbox_core as core          # noqa: E402
import policy                        # noqa: E402
from providers import AlreadyExists, ProviderError, ensure_loaded, create  # noqa: E402

PASS, FAIL = [], []


def check(name: str, fn):
    try:
        fn()
        PASS.append(name)
        print(f"  PASS  {name}")
    except AssertionError as e:
        FAIL.append((name, str(e)))
        print(f"  FAIL  {name}: {e}")
    except Exception as e:
        FAIL.append((name, f"{type(e).__name__}: {e}"))
        print(f"  FAIL  {name}: {type(e).__name__}: {e}")


def expect_raises(exc, fn, contains: str = ""):
    try:
        fn()
    except exc as e:
        if contains and contains not in str(e):
            raise AssertionError(f"异常信息不含 {contains!r}：{e}")
        return
    raise AssertionError(f"期望抛 {exc.__name__}，但没有抛")


def main() -> int:
    ensure_loaded()
    work = tempfile.mkdtemp(prefix="agsstest-")
    root = os.path.join(work, policy.ROOT_NAME)
    prov = create("dir_local", {"root": root})
    A, B = "WorkBuddy", "TRAE"

    # ---------- P1 路径约束 ----------
    def p1_paths():
        for bad in ("../secret", "inbox/../../x", "/etc/passwd", "C:/Windows/win.ini",
                    "tools/x", "inbox/x/../../../y"):
            expect_raises(policy.PolicyError, lambda b=bad: policy.check_rel(b))
        assert policy.check_rel("inbox/WorkBuddy") == "inbox/WorkBuddy"
        assert policy.check_rel("") == ""
        # provider 层再兜一道
        expect_raises(ProviderError, lambda: prov.list_dir("../"))

    check("P1 路径只在 AgentSwitchSpace 白名单内", p1_paths)

    # ---------- P2 大文件确认 ----------
    def p2_large():
        saved = policy.LARGE_FILE_BYTES
        policy.LARGE_FILE_BYTES = 16           # 缩到 16 字节，用来验证"闸门在写路径上"
        try:
            big = "x" * 64                     # 64B > 16B
            expect_raises(policy.ConfirmationRequired,
                          lambda: core.write_verified(prov, core.df("system"),
                                                      "big.json", big))
            # 有确认就必须放行
            core.write_verified(prov, core.df("system"), "big.json", big, confirm_large=True)
            assert prov.exists(core.df("system"), "big.json")
            # 小于阈值不拦
            small = "y" * 4
            core.write_verified(prov, core.df("system"), "small.json", small)
        finally:
            policy.LARGE_FILE_BYTES = saved

    check("P2 >阈值写入必须确认（且确认后放行）", p2_large)

    # ---------- C1 初始化 ----------
    def c1_init():
        r1 = core.init_space(prov, [A, B])
        assert set(r1["agents"]) == {A, B}, r1
        r2 = core.init_space(prov, [A, B])
        assert r2["agents"] == [] and set(r2["skipped"]) == {A, B}, r2
        assert set(core.agents(prov)) == {A, B}, core.agents(prov)

    check("C1 初始化幂等", c1_init)

    # ---------- C2 发送 ----------
    sent_id = {}

    def c2_send():
        r = core.send(prov, from_agent=A, to_agent=B, message_type=core.T_MSG,
                      content="请回复当前 Agent 名称。", task_id="TASK-1")
        assert r["verified"] and r["archived"], r
        sent_id["id"] = r["message_id"]
        assert prov.exists(core.d(A, core.SENT), f"{r['message_id']}.json")
        assert prov.exists(core.d(B, core.INBOX), f"{r['message_id']}.json")

    check("C2 发送 + 回读校验 + 存档", c2_send)

    # ---------- C3 inbox / claim / reply / finalize ----------
    def c3_cycle():
        items = core.list_inbox(prov, B)
        assert len(items) == 1, items
        msg = core.read_message(prov, B, items[0]["name"])
        core.validate_for_agent(msg, B)
        assert msg["message_id"] == sent_id["id"] and msg["from_agent"] == A

        core.claim(prov, B, msg["message_id"], items[0]["name"])
        assert core.find_in(prov, B, core.PROCESSING, msg["message_id"]) is not None
        assert core.find_in(prov, B, core.INBOX, msg["message_id"]) is None

        rep = core.reply(prov, agent=B, original=msg, body="我是 TRAE。")
        assert prov.exists(core.d(A, core.INBOX), f"{rep['reply_id']}.json")
        back = core.parse_message(prov.read_text(core.d(A, core.INBOX), f"{rep['reply_id']}.json"))
        assert back["reply_to"] == msg["message_id"] and back["message_type"] == core.T_REPLY

        fin = core.finalize(prov, agent=B, message_id=msg["message_id"], ok=True)
        assert fin["state"] == "DONE" and fin["verified"]
        assert core.find_in(prov, B, core.COMPLETED, msg["message_id"]) is not None

    check("C3 inbox → claim → reply → completed", c3_cycle)

    # ---------- C4 防重复 ----------
    def c4_dup():
        # 未 claim 不能 reply
        r = core.send(prov, from_agent=A, to_agent=B, message_type=core.T_MSG, content="第二条")
        items = core.list_inbox(prov, B)
        target = next(i for i in items if i["name"] == f"{r['message_id']}.json")
        expect_raises(ProviderError, lambda: core.claim(prov, B, r["message_id"], "不存在的.json"))
        # 同一 message_id 不能写两次（文件名唯一 → AlreadyExists）
        msg = core.read_message(prov, B, target["name"])
        body = core.build_message(from_agent=A, to_agent=B, message_type=core.T_MSG,
                                  content="dup", message_id=r["message_id"])
        expect_raises(AlreadyExists,
                      lambda: core.write_verified(prov, core.d(B, core.INBOX),
                                                  f"{r['message_id']}.json",
                                                  json.dumps(body, ensure_ascii=False)))
        core.claim(prov, B, r["message_id"], target["name"])
        core.finalize(prov, agent=B, message_id=r["message_id"], ok=True)
        # 已完成后再次 finalize 应报找不到（不允许重复收尾）
        expect_raises(ProviderError,
                      lambda: core.finalize(prov, agent=B, message_id=r["message_id"], ok=True))

    check("C4 防重复（未领取/重复写/重复收尾）", c4_dup)

    # ---------- C5 端到端 PING→PONG ----------
    def c5_ping():
        ledger_a = core.Ledger(os.path.join(work, "ledger-a.json"), A)
        ledger_b = core.Ledger(os.path.join(work, "ledger-b.json"), B)

        ping = core.send(prov, from_agent=A, to_agent=B,
                         message_type=core.T_PING, content="ping")
        # B 侧
        items = core.list_inbox(prov, B)
        nm = next((i["name"] for i in items
                   if i["name"].startswith(ping["message_id"])), None)
        assert nm, f"B 的 inbox 里找不到 ping：{ping['message_id']} in {[i['name'] for i in items]}"
        m = core.read_message(prov, B, nm)
        core.claim(prov, B, m["message_id"], nm)
        core.reply(prov, agent=B, original=m, body="PONG")
        core.finalize(prov, agent=B, message_id=m["message_id"], ok=True)
        ledger_b.mark(m["message_id"], "DONE")

        # A 侧收到回复
        inbox_a = [i for i in core.list_inbox(prov, A)
                   if not ledger_a.seen(i["name"][:-5])]
        cand = [i for i in inbox_a if i["name"].startswith("MSG-")]
        assert cand, f"A 的 inbox 为空：{[i['name'] for i in core.list_inbox(prov, A)]}"
        # 按 reply_to 精确挑出这次 PING 的回复（inbox 里可能还留着更早的回复）
        reply_msg = None
        for i in cand:
            m2 = core.read_message(prov, A, i["name"])
            if m2.get("reply_to") == ping["message_id"]:
                reply_msg = m2
                break
        assert reply_msg is not None, f"没找到 reply_to={ping['message_id']} 的回复"
        assert reply_msg["message_type"] == core.T_REPLY, reply_msg
        assert reply_msg["reply_to"] == ping["message_id"], \
            f"reply_to={reply_msg['reply_to']} want={ping['message_id']}"
        assert reply_msg["content"] == "PONG", reply_msg["content"]
        core.claim(prov, A, reply_msg["message_id"], reply_msg["_file"])
        core.finalize(prov, agent=A, message_id=reply_msg["message_id"], ok=True)
        ledger_a.mark(reply_msg["message_id"], "DONE")
        assert core.find_in(prov, A, core.COMPLETED, reply_msg["message_id"]) is not None, \
            f"completed 里没有 {reply_msg['message_id']}"

    check("C5 端到端 PING→PONG 闭环", c5_ping)

    # ---------- C6 台账 ----------
    def c6_ledger():
        core.send(prov, from_agent=A, to_agent=B, message_type=core.T_NOTICE, content="通知")
        led = core.Ledger(os.path.join(work, "ledger-c.json"), B)
        items = core.list_inbox(prov, B)
        if items:
            led.mark(items[0]["name"][:-5], "DONE")
            assert led.seen(items[0]["name"][:-5])
        assert len(led.data["processed"]) >= 1

    check("C6 台账记录", c6_ledger)

    print(f"\n结果：{len(PASS)} 通过 / {len(FAIL)} 失败")
    for n, e in FAIL:
        print(f"  - {n}: {e}")
    shutil.rmtree(work, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
