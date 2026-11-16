#!/usr/bin/env python3
"""夸克网盘最小可行性探测（需求文档「阶段一」最小测试程序）。

目的：用真实调用回答 8 个问题，而不是靠文档假设。
  1. 能否创建目录（含多层路径）？
  2. 能否上传文件并拿到 fid？
  3. 上传后多久能在目录列表里"发现"？（可见性时延）
  4. 下载回来的内容是否逐字节一致（JSON 完整性）？
  5. 同名文件重复上传是覆盖还是自动改名？（决定"能不能原地改状态"）
  6. 能否移动文件？（决定"目录即状态"是否可行）
  7. 移动是否可验证（源目录消失 + 目标目录出现）？
  8. 各操作耗时量级。

输出：Windows 控制台摘要 + 同目录 evidence-<时间戳>.jsonl 逐条证据。
用法： python probe_quark.py [--root AgentMailboxProbe] [--keep]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone

NODE = "node"
CLI = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..",
                   "_vendor", "quark", "scripts", "quark-drive.cjs")
SESSION_INPUT = "quark mailbox 技术验证"
SESSION_ID = "1794840592-hauwfb"

EVIDENCE: list[dict] = []


class CliError(RuntimeError):
    def __init__(self, action: str, code, msg):
        super().__init__(f"{action} failed: code={code} msg={msg}")
        self.action, self.code, self.msg = action, code, msg


def run(args: list[str], timeout: int = 300) -> tuple[dict, list[dict], float]:
    """调用官方 CLI，返回 (result 行, 全部 NDJSON 行, 秒数)。"""
    cmd = [NODE, CLI, "--session-input", SESSION_INPUT, "--session-id", SESSION_ID] + args
    t0 = time.time()
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=timeout)
    elapsed = time.time() - t0
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
        raise CliError(args[0], "no_result", (p.stdout or p.stderr)[-300:])
    if p.returncode != 0 or result.get("code") not in (0, None):
        raise CliError(args[0], result.get("code"), result.get("msg"))
    return result, lines, elapsed


def ev(step: str, **kw) -> None:
    rec = {"ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
           "step": step, **kw}
    EVIDENCE.append(rec)
    print(f"[{step}] " + " ".join(f"{k}={v}" for k, v in kw.items() if k != "detail"))


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def browse(fid: str) -> list[dict]:
    """列目录（一次性取全部直接子项，读 artifact 保证完整）。"""
    res, lines, secs = run(["browse", "--parent-fid", fid, "--all"])
    items = [x["data"] for x in lines if x.get("type") == "list"]
    art = next((x for x in lines if x.get("type") == "artifact"), None)
    if art:
        path = art["data"]["file_path"]
        tmp = path + ".utf8"
        # artifact 可能是带 BOM/无 BOM 的 UTF-8，按字节读再解码，避免编码猜测
        with open(path, "rb") as fh:
            raw = fh.read()
        text = raw.decode("utf-8-sig")
        items = [json.loads(l) for l in text.splitlines() if l.strip()]
    return items


def find(items: list[dict], name: str) -> dict | None:
    for it in items:
        if it.get("filename") == name:
            return it
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="AgentMailboxProbe")
    ap.add_argument("--keep", action="store_true", help="保留探测文件（默认保留，仅提示）")
    a = ap.parse_args()

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f"evidence-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
    tmpdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
    os.makedirs(tmpdir, exist_ok=True)
    verdict: dict[str, object] = {}

    try:
        # ---- 0. 身份 ----
        res, _, secs = run(["get-user-info"])
        info = res.get("data", {})
        ev("identity", nickname=info.get("userInfo", {}).get("nickname"),
           vip=info.get("vipInfo", {}).get("vipType"), secs=round(secs, 2))

        # ---- 1. 多层路径创建（一次调用建三段）----
        t0 = time.time()
        res, _, secs = run(["create-folder", "--dir-path", f"{a.root}/probe", "--parent-fid", "0"])
        root_fid = res["data"]["fid"]
        root_path = res["data"].get("full_path", "")
        ev("create_root", fid=root_fid, full_path=root_path, secs=round(secs, 2))
        verdict["create_folder"] = True

        # 幂等：同路径再建一次，应返回同一 fid
        res2, _, _ = run(["create-folder", "--dir-path", f"{a.root}/probe", "--parent-fid", "0"])
        ev("create_idempotent", same_fid=(res2["data"]["fid"] == root_fid), fid=res2["data"]["fid"])
        verdict["create_idempotent"] = res2["data"]["fid"] == root_fid

        # 子目录（单层）
        res, _, secs = run(["create-folder", "--dir-path", "inbox-test", "--parent-fid", root_fid])
        inbox_fid = res["data"]["fid"]
        ev("create_subdir", fid=inbox_fid, full_path=res["data"].get("full_path"), secs=round(secs, 2))

        # ---- 2. 上传 ----
        rnd = random.randint(100000, 999999)
        msg = {
            "protocol_version": "0.1",
            "message_id": f"MSG-PROBE-{rnd}",
            "from_agent": "WorkBuddy",
            "to_agent": "Probe",
            "message_type": "PING",
            "content": f"随机数 {rnd} / sha 校验载荷",
            "status": "NEW",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "reply_to": None,
            "task_id": None,
            "attachments": [],
            "metadata": {"probe": True, "payload": "中文与 emoji ✅ 混排"},
        }
        text = json.dumps(msg, ensure_ascii=False, indent=2)
        fname = f"{msg['message_id']}.json"
        local = os.path.join(tmpdir, fname)
        with open(local, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)

        res, lines, secs = run(["upload", local, "--parent-fid", inbox_fid])
        d = res["data"]
        up_fid = (d.get("fids") or [None])[0]
        ev("upload", fid=up_fid, name=d.get("fileNames"), size=d.get("totalSize"),
           full_path=d.get("fullPath"), secs=round(secs, 2), sha256=sha(text)[:16])
        verdict["upload"] = bool(up_fid)

        # ---- 3. 可见性时延：上传后多久能被列表发现 ----
        lat = None
        for i in range(0, 31):
            items = browse(inbox_fid)
            if find(items, fname):
                lat = i
                break
            time.sleep(1)
        ev("visibility_lag", name=fname, seconds=lat)
        verdict["visibility_lag_sec"] = lat

        # ---- 4. 下载并逐字节校验 ----
        items = browse(inbox_fid)
        it = find(items, fname)
        dl_dir = os.path.join(tmpdir, "dl")
        os.makedirs(dl_dir, exist_ok=True)
        res, _, secs = run(["download", "--fid", it["fid"], "--output-dir", dl_dir, "--overwrite"])
        got_path = None
        for root_, _, files in os.walk(dl_dir):
            if fname in files:
                got_path = os.path.join(root_, fname)
        if got_path is None:
            ev("download", ok=False, detail="file not found in output dir", secs=round(secs, 2))
            verdict["download"] = False
        else:
            with open(got_path, "rb") as fh:
                got = fh.read().decode("utf-8-sig")
            same = got == text
            ev("download", ok=True, byte_identical=same, sha_local=sha(text)[:16],
               sha_remote=sha(got)[:16], bytes=len(got.encode("utf-8")), secs=round(secs, 2))
            verdict["download_identical"] = same

        # ---- 5. 同名文件重复上传（覆盖 or 改名）----
        # 关键：本地文件名必须与远端现有文件名**完全一致**，才是真正的同名上传测试
        v2 = text.replace(str(rnd), str(rnd + 1))
        dup_dir = os.path.join(tmpdir, "dup", f"{rnd}")
        os.makedirs(dup_dir, exist_ok=True)
        local2 = os.path.join(dup_dir, fname)
        with open(local2, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(v2)
        try:
            res, _, secs = run(["upload", local2, "--parent-fid", inbox_fid])
            ev("upload_same_name", ok=True, fids=res["data"].get("fids"),
               names=res["data"].get("fileNames"), instant=res["data"].get("instantUpload"),
               secs=round(secs, 2))
        except CliError as e:
            ev("upload_same_name", ok=False, code=e.code, msg=e.msg)
        time.sleep(1)
        items = browse(inbox_fid)
        names = sorted(i["filename"] for i in items)
        orig = find(items, fname)
        overwritten = bool(orig and orig.get("size") == len(v2.encode("utf-8")))
        ev("same_name_result", names=names, orig_size=orig.get("size") if orig else None,
           new_size=len(v2.encode("utf-8")), overwritten=overwritten)
        verdict["same_name_names"] = names
        verdict["same_name_overwrote"] = overwritten

        # ---- 6. 移动 + 7. 可验证性 ----
        res, _, secs = run(["create-folder", "--dir-path", "processing-test", "--parent-fid", root_fid])
        proc_fid = res["data"]["fid"]
        target = find(browse(inbox_fid), fname)
        run(["move", target["fid"], "--target-fid", proc_fid])
        time.sleep(1)
        in_target = find(browse(proc_fid), fname) is not None
        gone_from_src = find(browse(inbox_fid), fname) is None
        ev("move", ok=in_target and gone_from_src, present_in_target=in_target,
           absent_in_source=gone_from_src, secs=round(secs, 2))
        verdict["move"] = in_target and gone_from_src

        # ---- 8. 搜索（可能被用于替代遍历）----
        try:
            res, lines, secs = run(["search", "--keyword", fname[:30]])
            hits = (res.get("data") or {}).get("file_list") or []
            ev("search", hits=len(hits), first=hits[0]["filename"] if hits else None,
               secs=round(secs, 2))
            verdict["search"] = len(hits) > 0
        except CliError as e:
            ev("search", ok=False, code=e.code, msg=e.msg)
            verdict["search"] = False

    finally:
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            for rec in EVIDENCE:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print("\n===== 结论 =====")
        for k, v in verdict.items():
            print(f"  {k}: {v}")
        print(f"\n证据文件: {out}")

    ok = all(bool(v) for k, v in verdict.items() if k != "visibility_lag_sec")
    ok = ok and verdict.get("visibility_lag_sec") is not None
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
