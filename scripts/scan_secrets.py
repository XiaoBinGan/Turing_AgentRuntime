#!/usr/bin/env python3
"""开源前的密钥扫描：找**真**密钥，不报测试假值。

朴素正则在这里是负资产：它会把 `sk-abcdefghijklmnop` 这种测试假值和
「用于识别私钥的正则表达式本身」都标成泄漏。假阳性多了，这条检查就会被无视，
而那正是最危险的时刻。

所以判定用两项启发：
  * 随机性：真密钥没有长串连续字母表序列（`abcdefgh`、`12345678`）
  * 体量：真私钥的 base64 体在 1500 字符以上；fixture 通常几十字符

退出码 0 = 干净，1 = 命中。命中时只打印位置，**不打印内容**（避免把密钥
写进 CI 日志，那等于又泄漏一次）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SKIP_DIRS = {".venv", ".git", ".internal", "__pycache__", "node_modules",
             "site", "build", "dist"}
SKIP_SUFFIX = {".pyc", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".zip", ".gz"}

#: 明确允许的测试假值（团队约定：假密钥一律用这些）
FIXTURE_ALLOW = {
    # 字母表序列（明显是人敲的）
    "sk-abcdefghijklmnopqrstuvwxyz012345",
    "sk-ABCDEFGHIJKLMNOP1234",
    "sk-ABCDEFGHIJKLMNOPQRSTUVWX",
    # 字母数字交替（人工构造的"看起来随机"假值）
    "sk-a1B2c3D4e5F6g7H8i9J0",
    # AWS 官方文档示例 key（永远是这一个）
    "AKIAIOSFODNN7EXAMPLE",
    "AKIAABCDEFGHIJKLMNOP",
    # 占位符
    "sk-test", "sk-xxx", "sk-dummy", "sk-fake", "sk-placeholder",
}

_KEY_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9]{20,}"),
    re.compile(r"\bgho_[A-Za-z0-9]{30,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),                     # AWS access key id
]
_PEM = re.compile(r"-----BEGIN[^-]{0,60}PRIVATE KEY-----(.*?)-----END", re.S)


def _looks_random(token: str) -> bool:
    """真密钥的特征：没有长串连续字母表序列。"""
    if token in FIXTURE_ALLOW:
        return False
    body = token.split("-", 1)[-1]
    # 连续升序字母/数字（abcdef、123456、qwerty 前 8 位不算，只查字母与数字）
    for run in re.findall(r"[a-z]{8,}|[A-Z]{8,}|[0-9]{8,}", body):
        if run in ("abcdefghijklmnopqrstuvwxyz", "ABCDEFGHIJKLMNOPQRSTUVWXYZ") \
                or run.lower() in ("abcdefgh", "12345678", "abcdefghij"):
            return False
        # 一般化：连续字符比递增
        if all(ord(b) - ord(a) == 1 for a, b in zip(run, run[1:])):
            return False
    return len(body) >= 20


def scan(root: Path) -> list[str]:
    hits: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix in SKIP_SUFFIX:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        rel = path.relative_to(root)
        for pat in _KEY_PATTERNS:
            for m in pat.finditer(text):
                if _looks_random(m.group(0)):
                    line = text.count("\n", 0, m.start()) + 1
                    hits.append(f"{rel}:{line} 疑似密钥（{m.group(0)[:8]}…）")
        for m in _PEM.finditer(text):
            body = m.group(1)
            # 真私钥 base64 体 >1500；fixture 通常几十字符
            if len(body.strip()) > 800:
                line = text.count("\n", 0, m.start()) + 1
                hits.append(f"{rel}:{line} 疑似私钥块（体长 {len(body.strip())}）")
    return hits


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    hits = scan(root)
    if hits:
        print("发现疑似凭据：", file=sys.stderr)
        for h in hits:
            print(f"  {h}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
