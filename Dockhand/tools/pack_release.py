#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""打包上架 zip，并在打包前后做发布前校验。

为什么要脚本：手工 zip 迟早会把 config.toml（含 token）、*.bak、__pycache__
或者 real 实拍包一起塞进去。这里用白名单，只放该放的东西。

校验失败一律非零退出，不生成 zip —— 宁可不出包，也不出带占位符的包。
"""

import argparse
import hashlib
import json
import os
import re
import sys
import zipfile

# 只放行这些路径（相对插件根）。real 包不在其中：它 license=private，不对外分发。
ALLOW_FILES = [
    "_manifest.json",
    "plugin.py",
    "README.md",
    "CHANGELOG.md",
    "LICENSE",
    "SECURITY.md",
    "config.example.toml",
    "engine/",
    "game_adapter/__init__.py",
    "game_adapter/base.py",
    "game_adapter/demo/",
    "docs/",
]

BAD_PATH_PATTERNS = [
    r"__pycache__",
    r"\.pyc$",
    r"\.bak",
    r"\.orig$",
    r"(^|/)config\.toml$",          # 含 token，绝不能进包
    r"game_adapter/real",           # 实拍包不分发
    r"(^|/)shots?/",
    r"(^|/)dist/",
    r"\.log$",
]

# 占位符检查：命中即拦截。发布前必须换成真实值。
PLACEHOLDER_PATTERNS = [
    (r"example\.com", "占位域名 example.com"),
    (r"example\.org", "占位域名 example.org"),
    (r"\bTODO\b", "未完成的 TODO 标记"),
    (r"\bFIXME\b", "未完成的 FIXME 标记"),
    (r"<[^<>\n]{0,40}待填[^<>\n]{0,40}>", "待填占位"),
    (r"your[-_]?token|YOUR_TOKEN", "示例 token 占位"),
]

TEXT_EXT = {".json", ".py", ".md", ".toml", ".txt", ".yaml", ".yml", ".cfg", ".ini"}


def iter_whitelisted(src):
    """按白名单展开，产出 (绝对路径, 包内相对路径)。"""
    for entry in ALLOW_FILES:
        full = os.path.join(src, entry.rstrip("/"))
        if not os.path.exists(full):
            continue
        if os.path.isdir(full):
            for root, dirs, files in os.walk(full):
                dirs[:] = [d for d in dirs if d != "__pycache__"]
                for fn in sorted(files):
                    ap = os.path.join(root, fn)
                    yield ap, os.path.relpath(ap, src).replace("\\", "/")
        else:
            yield full, entry


def scan_text(ap, rel, problems):
    ext = os.path.splitext(ap)[1].lower()
    if ext not in TEXT_EXT:
        return
    try:
        with open(ap, "r", encoding="utf-8", errors="replace") as f:
            body = f.read()
    except Exception as exc:  # noqa: BLE001
        problems.append("%s：读取失败 %s" % (rel, exc))
        return
    for pat, desc in PLACEHOLDER_PATTERNS:
        for m in re.finditer(pat, body):
            line_no = body[: m.start()].count("\n") + 1
            line = body.splitlines()[line_no - 1].strip() if line_no <= len(body.splitlines()) else ""
            problems.append("%s:%d %s -> %s" % (rel, line_no, desc, line[:120]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    src = os.path.abspath(args.src)
    manifest_path = os.path.join(src, "_manifest.json")
    if not os.path.isfile(manifest_path):
        print("找不到 _manifest.json：%s" % src)
        return 2
    manifest = json.load(open(manifest_path, encoding="utf-8"))
    version = str(manifest.get("version") or "0.0.0")

    files = list(iter_whitelisted(src))
    problems = []
    seen = set()
    uniq = []

    for ap_, rel in files:
        if rel in seen:
            continue
        seen.add(rel)
        uniq.append((ap_, rel))
        for pat in BAD_PATH_PATTERNS:
            if re.search(pat, rel):
                problems.append("%s：不该进包（命中排除规则 %s）" % (rel, pat))
                break
        scan_text(ap_, rel, problems)

    # 清单层面的硬要求
    if not any(rel.startswith("game_adapter/demo/") for _, rel in files):
        problems.append("包内没有 game_adapter/demo/：社区只收 demo 级包")
    if any(rel.startswith("game_adapter/real") for _, rel in files):
        problems.append("包内含 real 实拍包：license=private，不得分发")
    if not str(manifest.get("author", {}).get("url", "")).strip():
        problems.append("_manifest.json 的 author.url 为空")

    print("包内文件 %d 个，版本 %s" % (len(seen), version))
    if problems:
        print("\n发布前校验未通过（%d 项），不生成 zip：" % len(problems))
        for p in problems:
            print("  - " + p)
        return 1

    out_dir = os.path.join(src, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out_zip = args.out or os.path.join(out_dir, "shipctl-%s.zip" % version)
    with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as zf:
        for ap_, rel in sorted(uniq, key=lambda x: x[1]):
            zf.write(ap_, rel)
    digest = hashlib.sha256(open(out_zip, "rb").read()).hexdigest()
    print("已生成 %s" % out_zip)
    print("SHA256 %s" % digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
