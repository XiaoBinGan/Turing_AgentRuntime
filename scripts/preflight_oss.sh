#!/usr/bin/env bash
# 开源前自检：确认没有不该公开的东西进仓库。
#
# 为什么需要脚本而不是靠人记：泄漏密钥、泄漏内网地址这类事故
# 都是一次疏忽，且事后无法撤回（fork 已经散出去了）。
set -uo pipefail
cd "$(dirname "$0")/.."
FAIL=0
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"
say() { printf '%-46s %s\n' "$1" "$2"; }
bad() { say "$1" "FAIL"; FAIL=1; }
ok()  { say "$1" "ok"; }

# ① 真实密钥不得落盘
#
# 朴素正则会把**测试假值**和**检测密钥的正则本身**都报出来 ——
# 假阳性会让这条检查被无视，那比没有检查更糟。
# 所以用启发式：真密钥是随机的；`sk-abcdefghij...` 这类字母表序列不是。
if "$PY" scripts/scan_secrets.py 2>/dev/null; then
  ok "无真实密钥"
else
  bad "无真实密钥"
fi

# ③ 内网地址（排除文档里作为示例的 127.0.0.1 / localhost）
if grep -rInE 'https?://(10|192\.168|172\.(1[6-9]|2[0-9]|3[01]))\.[0-9]' \
     --exclude-dir=.venv --exclude-dir=.git --exclude-dir=.internal . 2>/dev/null | grep -q .; then
  bad "无内网地址"
else
  ok "无内网地址"
fi

# ④ 内部工作文件不得在版本控制范围内
for f in .internal BRIEF.md README.legacy.md ARCH-CHECKLIST.md; do
  if [ -e "$f" ] && git ls-files --error-unmatch "$f" >/dev/null 2>&1; then
    bad "内部文件未入库: $f"
  fi
done
ok "内部文件隔离"

# ⑤ 大文件（GitHub 单文件 100MB 硬限，提前在 5MB 卡）
BIG=$(find . -type f -size +5M -not -path "./.venv/*" -not -path "./.git/*" 2>/dev/null)
if [ -n "$BIG" ]; then bad "无 >5MB 文件"; echo "$BIG" | sed 's/^/    /'; else ok "无 >5MB 文件"; fi

# ⑥ 许可证与必需文档
for f in LICENSE README.md CONTRIBUTING.md requirements-core.txt; do
  [ -f "$f" ] || bad "缺少 $f"
done
ok "必需文档齐备"

# ⑦ 测试全绿才能发
if ./verify.sh >/tmp/preflight-verify.log 2>&1; then
  ok "verify.sh 全绿"
else
  bad "verify.sh 未通过（见 /tmp/preflight-verify.log）"
fi

echo
[ "$FAIL" -eq 0 ] && echo "可以发布。" || echo "存在阻塞项，不要发布。"
exit "$FAIL"
