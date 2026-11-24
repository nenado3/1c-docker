#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
GUI_SCRIPT="$PROJECT_DIR/gui.sh"

if [[ ! -f "$GUI_SCRIPT" ]]; then
  echo "Не найден файл запуска панели: $GUI_SCRIPT" >&2
  exit 1
fi

if command -v xdg-user-dir >/dev/null 2>&1; then
  DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
fi
if [[ -z "${DESKTOP_DIR:-}" || ! -d "$DESKTOP_DIR" ]]; then
  if [[ -d "$HOME/Рабочий стол" ]]; then
    DESKTOP_DIR="$HOME/Рабочий стол"
  elif [[ -d "$HOME/Desktop" ]]; then
    DESKTOP_DIR="$HOME/Desktop"
  else
    DESKTOP_DIR="$HOME"
  fi
fi

mkdir -p "$DESKTOP_DIR"
LAUNCHER="$DESKTOP_DIR/Менеджер 1С.desktop"
# Escape backslashes and double quotes for the Desktop Entry Exec field.
ESCAPED_DIR=${PROJECT_DIR//\\/\\\\}
ESCAPED_DIR=${ESCAPED_DIR//\"/\\\"}
cat > "$LAUNCHER" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Менеджер 1С
Comment=Запуск веб-панели управления 1С Docker
Exec=/bin/bash -c "cd '$ESCAPED_DIR' && exec ./gui.sh"
Icon=web-browser
Terminal=true
Categories=Development;System;
EOF
chmod +x "$LAUNCHER"

if command -v gio >/dev/null 2>&1; then
  gio set "$LAUNCHER" metadata::trusted true 2>/dev/null || true
fi

echo "Ярлык создан: $LAUNCHER"
echo "Для запуска дважды щёлкните «Менеджер 1С» на рабочем столе."
