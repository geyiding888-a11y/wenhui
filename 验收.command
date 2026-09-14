#!/bin/bash
# Mac 上双击这个文件自检（不花钱）。
# 中文提示统一在 src/wenhui/launcher.py 里。

cd "$(dirname "$0")" || exit 1

if ! command -v uv >/dev/null 2>&1; then
    cat "config/no-uv.txt"
    echo
    read -r -p "  按回车关闭…"
    exit 1
fi

# --extra dev：pytest 在 dev 这个可选组里，光 uv sync 不会装它
if ! uv sync --quiet --extra dev; then
    cat "config/sync-failed.txt"
    echo
    read -r -p "  按回车关闭…"
    exit 1
fi

uv run --quiet python -X utf8 -m wenhui.launcher selfcheck

echo
read -r -p "  按回车关闭…"
