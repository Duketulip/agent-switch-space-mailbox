#!/usr/bin/env python3
"""语义补充探测：同名上传是否覆盖、move 撞名行为、search 稳定性。

自动发现目录，不依赖上一次 probe 的具体 fid：
  根 fid(0) → AgentMailboxProbe2 → {probe/…/inbox-test, processing-test}

用法：python probe_semantics.py [--root AgentMailboxProbe2]
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from datetime import datetime

from probe_quark import CliError, browse, find, run, sha, ev, EVIDENCE


def download(fid: str, name: str, workdir: str) -> "str | None":
    if os.path.isdir(workdir):
        shutil.rmtree(workdir, ignore_errors=True)
    os.makedirs(workdir, exist_ok=True)
    run(["download", "--fid", fid, "--output-dir", workdir, "--overwrite"])
    hits = []
    for root, _, files in os.walk(workdir):
        hits += [os.path.join(root, f) for f in files if f != ".DS_Store"]
    exact = [h for h in hits if os.path.basename(h) == name]
    return (exact or hits or [None])[0]


def read_text(p: "str | None") -> "str | None":
    if not p:
        return None
    with open(p, "rb") as fh:
        return fh.read().decode("utf-8-sig")


def walk_to(fid: str, name: str, depth: int = 3, _seen=None) -> "str | None":
    """在目录树里按名字找目录 fid（网盘没有递归列表，靠逐层 browse）。"""
    _seen = _seen or set()
    if fid in _seen or depth < 0:
        return None
    _seen.add(fid)
    for it in browse(fid):
        if it.get("filename") == name:
            return it["fid"]
    for it in browse(fid):
        if it.get("file_type") == "0" or it.get("category") == 0:
            got = walk_to(it["fid"], name, depth - 1, _seen)
            if got:
                return got
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="AgentMailboxProbe2")
    a = ap.parse_args()

    out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       f"evidence-semantics-{datetime.now().strftime('%Y%m%d-%H%M%S')}.jsonl")
    work = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp2")
    verdict = {}

    try:
        root_fid = walk_to("0", a.root)
        inbox_fid = walk_to(root_fid, "inbox-test") or walk_to(root_fid, "probe")
        proc_fid = walk_to(root_fid, "processing-test")
        ev("discover", root=root_fid, inbox_test=inbox_fid, processing_test=proc_fid)

        dup_items = browse(inbox_fid)
        dup_name = next((i["filename"] for i in dup_items if "(1)" in i["filename"]), None)
        orig_name = dup_name.replace("(1)", "") if dup_name else None
        ev("dup_dir", dup_name=dup_name, orig_name=orig_name,
           names=[i["filename"] for i in dup_items])

        # 1) 同名上传后：原文件名（被移走的）内容 vs (1) 副本内容
        t_dup = read_text(download(find(dup_items, dup_name)["fid"], dup_name, os.path.join(work, "dup")))
        orig_items = browse(proc_fid) if proc_fid else []
        orig = find(orig_items, orig_name) if orig_name else None
        t_orig = read_text(download(orig["fid"], orig_name, os.path.join(work, "orig"))) if orig else None
        ev("same_name_after",
           dup_sha=sha(t_dup)[:16] if t_dup else None,
           orig_sha=sha(t_orig)[:16] if t_orig else None,
           identical=bool(t_dup and t_orig and t_dup == t_orig),
           orig_still_v1=bool(t_orig and f"-{orig_name.split('-')[-1].split('.')[0]}" in t_orig))
        verdict["same_name_no_overwrite"] = bool(t_dup and t_orig and t_dup != t_orig)

        # 2) move 撞名：把 (1) 副本移入一个已存在同名文件的目录
        tgt_res, _, _ = run(["create-folder", "--dir-path", f"mc-{int(time.time())}", "--parent-fid", root_fid])
        tgt_fid = tgt_res["data"]["fid"]
        seed_dir = os.path.join(work, "seed")
        os.makedirs(seed_dir, exist_ok=True)
        seed = os.path.join(seed_dir, dup_name)
        with open(seed, "w", encoding="utf-8", newline="\n") as fh:
            fh.write('{"seed": true}')
        run(["upload", seed, "--parent-fid", tgt_fid])
        time.sleep(1)
        before = sorted(i["filename"] for i in browse(tgt_fid))
        try:
            run(["move", find(dup_items, dup_name)["fid"], "--target-fid", tgt_fid])
            moved_ok = True
            err = None
        except CliError as e:
            moved_ok, err = False, f"{e.code}:{e.msg}"
        time.sleep(1)
        after = sorted(i["filename"] for i in browse(tgt_fid))
        renamed = after != sorted(before + [dup_name])
        ev("move_collision", before=before, after=after, move_ok=moved_ok, error=err, renamed=renamed)
        verdict["move_collision_renamed"] = renamed

        # 3) search 稳定性（同一关键词两次）
        for kw in ("MSG-PROBE", "  "):
            for attempt in (1, 2):
                try:
                    res, _, secs = run(["search", "--keyword", kw])
                    n = len((res.get("data") or {}).get("file_list") or [])
                    ev("search", keyword=kw, attempt=attempt, hits=n, secs=round(secs, 2))
                except CliError as e:
                    ev("search", keyword=kw, attempt=attempt, error_code=e.code, msg=e.msg)
    finally:
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            for rec in EVIDENCE:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print("\n===== 结论 =====")
        for k, v in verdict.items():
            print(f"  {k}: {v}")
        print(f"\n证据文件: {out}")
    return 0


if __name__ == "__main__":
    main()
