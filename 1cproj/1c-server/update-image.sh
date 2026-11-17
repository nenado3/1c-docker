#!/bin/bash
set -euo pipefail
. "$(dirname "$0")/common.sh"

echo "== Обновление базовых образов и пересборка сервера"
echo "== Шаг 1/2: пересборка с обновлением базовых образов"
$DOCKER compose build --pull --progress=plain postgres onec-server
echo "== Шаг 2/2: пересоздание контейнеров"
$DOCKER compose up -d --force-recreate postgres onec-server
echo "Обновление сервера завершено. Именованные volumes с данными не удалялись."
$DOCKER compose ps postgres onec-server
