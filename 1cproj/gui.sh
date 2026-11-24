#!/bin/bash
# Запуск графической панели управления контейнерами 1С (откроется в браузере)
cd "$(dirname "$0")"
command -v python3 >/dev/null 2>&1 || { echo "ОШИБКА: нужен python3 (sudo dnf install python3)" >&2; exit 1; }
exec python3 1c-manager/app.py "$@"
