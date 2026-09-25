#!/bin/bash
# Blur Faces Free — macOS 一键启动（使用项目自带 .venv，Python 3.11）
cd "$(dirname "$0")"
./.venv/bin/python blur_gui.pyw &
disown 2>/dev/null || true
sleep 0.4
exit 0
