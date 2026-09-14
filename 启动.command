#!/bin/bash
# Mac 上双击这个文件启动。
# 第一次双击如果被系统拦住，请按住 Control 点它，选「打开」，再确认一次。
#
# 中文提示统一放在 src/wenhui/launcher.py 里（Windows 和 Mac 共用一份，
# 改一处两边都变）。这个脚本只负责找到 uv、装好依赖，然后把活交给它。

cd "$(dirname "$0")" || exit 1

# 密钥可能存在这个文件里（在项目文件夹之外，权限只有你自己能读）
if [ -f "$HOME/.wenhui/env" ]; then
    # shellcheck disable=SC1091
    . "$HOME/.wenhui/env"
fi

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

uv run --quiet python -X utf8 -m wenhui.launcher start

echo
read -r -p "  按回车关闭…"
