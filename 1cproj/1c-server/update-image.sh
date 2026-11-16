#!/bin/bash
set -euo pipefail
. "$(dirname "$0")/common.sh"

echo "== Обновление базовых образов и пересборка сервера"
echo "== Шаг 1/3: получение обновлений базовых образов"
$DOCKER compose pull --ignore-buildable || echo "Предупреждение: сервисы собираются локально; продолжаем."
echo "== Шаг 2/3: пересборка образов с обновлёнными базовыми слоями"
$DOCKER compose --progress plain build --pull postgres onec-server
echo "== Шаг 3/3: пересоздание контейнеров"
$DOCKER compose up -d --force-recreate postgres onec-server
echo "Обновление сервера завершено. Именованные volumes с данными не удалялись."
$DOCKER compose ps postgres onec-server
