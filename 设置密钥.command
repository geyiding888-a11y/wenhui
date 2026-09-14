#!/bin/bash
# Mac 上双击这个文件设置 AI 密钥。只需要设置一次。
# 中文提示统一在 src/wenhui/launcher.py 里。

cd "$(dirname "$0")" || exit 1

if ! command -v uv >/dev/null 2>&1; then
    cat "config/no-uv.txt"
    echo
    read -r -p "  按回车关闭…"
    exit 1
fi

if ! uv sync --quiet; then
    cat "config/sync-failed.txt"
    echo
    read -r -p "  按回车关闭…"
    exit 1
fi

uv run --quiet python -X utf8 -m wenhui.launcher setkey

echo
read -r -p "  按回车关闭…"
