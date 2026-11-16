#!/bin/bash
. "$(dirname "$0")/common.sh"

read -r -p "Удалить контейнеры сервера И данные (базы, кластер)? Каталог licenses сохранится. [y/N] " ans
[ "$ans" = "y" ] || exit 0

$DOCKER compose down -v
echo "Готово. Дальше: ./start.sh"
