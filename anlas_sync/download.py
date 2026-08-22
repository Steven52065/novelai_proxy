#!/usr/bin/env python
"""从 novelai.net 下载前端 JS chunk，用于离线复现 anlas 计费逻辑。

用法:
    .\\.venv\\Scripts\\python.exe anlas_sync\\download.py

会自动:
1. 请求 https://novelai.net/image 获取 HTML
2. 从 HTML 中解析所有 /_next/static/chunks/*.js 的 URL
3. 按前缀挑选需要的 chunk 下载到 cache/ 目录（固定文件名）
4. 写入 cache/manifest.json 记录来源与下载时间

注意:
- novelai.net 的 CDN 会拦截默认 TLS 指纹的请求（httpx 会报 SSL 错误），
  因此这里使用 curl_cffi 并模拟 Chrome 136 的指纹。
- chunk 前缀（1052/7416/1741）对应当前前端中的计费模块，官网改版后
  需要按 anlas_sync/ANALYSIS.md 第 10 节重新定位并更新 NEEDED。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from pathlib import Path

from curl_cffi import requests

BASE = "https://novelai.net"
PAGE_URL = BASE + "/image"
CACHE_DIR = Path(__file__).resolve().parent / "cache"

# 需要的 chunk 前缀 -> 本地固定文件名
NEEDED = [
    ("/_next/static/chunks/webpack-", "webpack.js"),
    ("/_next/static/chunks/framework-", "framework.js"),
    ("/_next/static/chunks/main-", "main.js"),
    ("/_next/static/chunks/pages/_app-", "_app.js"),
    # 1052: GI/tY/H_ 定价模块(61225)、Dk/尺寸校验模块(57863)
    ("/_next/static/chunks/1052-", "chunk-1052.js"),
    # 7416: SW.getPrice (vibe 引用编码价格, 模块 25690)
    ("/_next/static/chunks/7416-", "chunk-7416.js"),
    # 1741: image 页主组件（角色引用附加费）
    ("/_next/static/chunks/1741-", "chunk-1741.js"),
]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(url: str, timeout: int = 120) -> bytes:
    r = requests.get(
        url,
        headers={"User-Agent": UA},
        timeout=timeout,
        allow_redirects=True,
        impersonate="chrome136",
    )
    r.raise_for_status()
    return r.content


def main() -> int:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"[1/2] 获取页面 {PAGE_URL}")
    html = fetch(PAGE_URL).decode("utf-8", errors="replace")

    # 收集 HTML 里出现的所有 chunk URL
    urls = sorted(set(re.findall(r"/_next/static/chunks/[A-Za-z0-9._/-]+\.js", html)))
    if not urls:
        print("错误: 未能从 HTML 解析到任何 chunk URL，页面结构可能已变化。", file=sys.stderr)
        return 1

    print(f"      页面共引用 {len(urls)} 个 chunk")

    manifest: dict = {"page": PAGE_URL, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                      "files": {}}
    changed: list[str] = []
    failures: list[str] = []

    for prefix, local_name in NEEDED:
        match = [u for u in urls if u.startswith(prefix)]
        if not match:
            print(f"  [跳过] 未找到 {prefix}*")
            failures.append(prefix)
            continue
        # 取第一个匹配（通常只有一个版本）
        chunk_url = BASE + match[0]
        target = CACHE_DIR / local_name
        print(f"[2/2] 下载 {match[0]} -> {local_name}")
        try:
            data = fetch(chunk_url)
        except Exception as e:  # noqa: BLE001
            print(f"  [失败] {e}", file=sys.stderr)
            failures.append(prefix)
            continue
        target.write_bytes(data)
        h = sha256(data)
        prev = manifest["files"].get(local_name, {}).get("sha256")
        if prev != h:
            changed.append(local_name)
        manifest["files"][local_name] = {"url": chunk_url, "size": len(data), "sha256": h}

    (CACHE_DIR / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print("\n下载完成。")
    if changed:
        print("本次有更新的文件:", ", ".join(changed))
    else:
        print("所有文件与上次一致，无更新。")
    if failures:
        print("缺失文件:", ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
