#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""用 GitHub Contents API 直接提交文件（不依赖 git 与 github.com 的 git 端口）。

为什么需要它
------------
本机到 github.com:443 的 git 连接会被间歇性重置（Recv failure / 连接超时），
但 api.github.com 一直可用。本脚本走 REST API 提交，绕开这个问题。

用法
----
1) 把一个 classic token（勾 repo）单独存成一行文本文件：
       C:\\Users\\<用户名>\\.gh_token.txt
   （用记事本存即可，脚本只从磁盘读，不打印、不回显）
2) 双击 dist\\push_via_api.cmd，或直接运行：
       python scripts/push_via_api.py

行为
----
- 逐个文件比较「本地内容 sha」与「远端 blob sha」，内容相同则跳过
- 不同的用 PUT 更新（远端已有则带 sha，实现更新而非新建）
- 全部完成后重新拉一次远端 sha 做校验
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import urllib.error
import urllib.request

OWNER = "newswire-lab"
REPO = "AIC-NewsBrief"
BRANCH = "main"
TOKEN_FILE = os.path.join(os.path.expanduser("~"), ".gh_token.txt")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "dist", "repo")

TARGETS = [
    "README.md",
    "docs/技术报告.md",
    "docs/技术报告.pdf",
    "docs/screenshots/3_brief.png",
    "docs/作品简介与名称.md",
]


def blob_sha(data: bytes) -> str:
    h = hashlib.sha1()
    h.update(b"blob %d\0" % len(data))
    h.update(data)
    return h.hexdigest()


def api(method: str, url: str, token: str, payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "AIC-NewsBrief-push")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.load(resp)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")[:300]
        return exc.code, {"error": body}


def read_token() -> str:
    if not os.path.exists(TOKEN_FILE):
        print("[!] 没找到 token 文件：%s" % TOKEN_FILE)
        print("    用记事本新建这个文件，把 ghp_... 一行粘进去并保存。")
        sys.exit(2)
    tok = io.open(TOKEN_FILE, encoding="utf-8").read().strip()
    if not tok.startswith(("ghp_", "github_pat_")):
        print("[!] token 文件内容不像 GitHub token（应以 ghp_ 或 github_pat_ 开头）")
        sys.exit(2)
    print("[i] 已从 %s 读取 token（长度 %d，不回显）" % (TOKEN_FILE, len(tok)))
    return tok


def main() -> int:
    token = read_token()

    st, me = api("GET", "https://api.github.com/user", token)
    if st != 200:
        print("[!] token 校验失败 HTTP %s：%s" % (st, me.get("error", "")))
        return 2
    print("[i] 已认证为：%s" % me.get("login"))

    changed = skipped = 0
    for rel in TARGETS:
        local = os.path.join(SRC, rel.replace("/", os.sep))
        if not os.path.exists(local):
            print("[!] 本地缺文件：%s" % rel)
            continue
        data = io.open(local, "rb").read()
        want = blob_sha(data)

        url = "https://api.github.com/repos/%s/%s/contents/%s" % (
            OWNER, REPO, urllib.request.quote(rel))
        st, cur = api("GET", url + "?ref=" + BRANCH, token)
        have = cur.get("sha") if st == 200 else None
        if have == want:
            print("[=] %-32s 已是最新，跳过（%d 字节）" % (rel, len(data)))
            skipped += 1
            continue

        payload = {
            "message": "update %s" % rel,
            "content": base64.b64encode(data).decode(),
            "branch": BRANCH,
        }
        if have:
            payload["sha"] = have
        st, res = api("PUT", url, token, payload)
        if st in (200, 201):
            print("[✓] %-32s 已提交（%d 字节，%s）"
                  % (rel, len(data), "更新" if have else "新建"))
            changed += 1
        else:
            print("[!] %-32s 提交失败 HTTP %s：%s" % (rel, st, res.get("error", "")))
            return 3

    print("\n提交 %d 个，跳过 %d 个。复核远端 sha ..." % (changed, skipped))
    ok = True
    for rel in TARGETS:
        local = os.path.join(SRC, rel.replace("/", os.sep))
        if not os.path.exists(local):
            continue
        want = blob_sha(io.open(local, "rb").read())
        url = "https://api.github.com/repos/%s/%s/contents/%s" % (
            OWNER, REPO, urllib.request.quote(rel))
        st, cur = api("GET", url + "?ref=" + BRANCH, token)
        have = cur.get("sha") if st == 200 else "ERR%s" % st
        flag = "一致" if have == want else "不一致"
        if have != want:
            ok = False
        print("  %-32s %s" % (rel, flag))

    print("\n%s" % ("[OK] 远端 4 个文件均已与本地一致。"
                    if ok else "[!] 仍有不一致，请重跑一次。"))
    return 0 if ok else 4


def delete_files(paths: list[str]) -> int:
    """删除远端文件（用于清理误传到仓库根目录的重复副本）。"""
    if not paths:
        print("[!] 没有指定要删除的路径")
        return 2
    token = read_token()
    st, me = api("GET", "https://api.github.com/user", token)
    if st != 200:
        print("[!] token 校验失败 HTTP %s：%s" % (st, me.get("error", "")))
        return 2
    print("[i] 已认证为：%s" % me.get("login"))

    ok = True
    for rel in paths:
        url = "https://api.github.com/repos/%s/%s/contents/%s" % (
            OWNER, REPO, urllib.request.quote(rel))
        st, cur = api("GET", url + "?ref=" + BRANCH, token)
        if st != 200:
            print("[i] %-28s 远端不存在，跳过" % rel)
            continue
        payload = {"message": "remove duplicate %s" % rel,
                   "sha": cur["sha"], "branch": BRANCH}
        st, res = api("DELETE", url, token, payload)
        if st == 200:
            print("[✓] %-28s 已删除" % rel)
        else:
            print("[!] %-28s 删除失败 HTTP %s：%s" % (rel, st, res.get("error", "")))
            ok = False

    print("\n复核 ...")
    for rel in paths:
        url = "https://api.github.com/repos/%s/%s/contents/%s?ref=%s" % (
            OWNER, REPO, urllib.request.quote(rel), BRANCH)
        st, _ = api("GET", url, token)
        print("  %-28s %s" % (rel, "已不存在" if st == 404 else "仍存在(HTTP %s)" % st))
    return 0 if ok else 3


def sync_tree(dry_run: bool = False) -> int:
    """比对 dist/repo 与远端 main 的全部文件，上传新增/变更项。"""
    token = read_token()
    st, me = api("GET", "https://api.github.com/user", token)
    if st != 200:
        print("[!] token 校验失败 HTTP %s：%s" % (st, me.get("error", "")))
        return 2
    print("[i] 已认证为：%s" % me.get("login"))

    tree = api("GET", "https://api.github.com/repos/%s/%s/git/trees/%s?recursive=1"
               % (OWNER, REPO, BRANCH))[1]
    remote = {x["path"]: x["sha"] for x in tree.get("tree", []) if x["type"] == "blob"}

    todo = []
    for dirpath, dirs, files in os.walk(SRC):
        dirs[:] = [d for d in dirs if d != ".git"]
        for f in files:
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, SRC).replace(os.sep, "/")
            lsha = blob_sha(io.open(p, "rb").read())
            if remote.get(rel) != lsha:
                todo.append((rel, p, lsha))

    print("[i] 远端 %d 个文件，本地 %d 个，需上传 %d 个"
          % (len(remote), sum(len(fs) for d, ds, fs in os.walk(SRC) if ".git" not in d), len(todo)))
    if dry_run or not todo:
        for rel, _p, _s in todo:
            print("    - %s" % rel)
        return 0

    ok = bad = 0
    for i, (rel, p, _lsha) in enumerate(todo, 1):
        data = io.open(p, "rb").read()
        url = "https://api.github.com/repos/%s/%s/contents/%s" % (
            OWNER, REPO, urllib.request.quote(rel))
        payload = {"message": "sync %s" % rel,
                   "content": base64.b64encode(data).decode(),
                   "branch": BRANCH}
        if rel in remote:
            payload["sha"] = remote[rel]
        st, res = api("PUT", url, token, payload)
        if st in (200, 201):
            ok += 1
            print("[%d/%d] ✓ %s" % (i, len(todo), rel))
        else:
            bad += 1
            print("[%d/%d] ✗ %s  HTTP %s %s" % (i, len(todo), rel, st, str(res.get("error", ""))[:120]))

    print("\n成功 %d / 失败 %d" % (ok, bad))
    return 0 if bad == 0 else 3

if __name__ == "__main__":
    argv = sys.argv[1:]
    if argv and argv[0] == "--delete":
        sys.exit(delete_files(argv[1:]))
    if argv and argv[0] == "--sync":
        sys.exit(sync_tree(dry_run="--dry-run" in argv))
    sys.exit(main())
