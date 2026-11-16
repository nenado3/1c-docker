#!/bin/bash
. "$(dirname "$0")/common.sh"

read -r -p "Удалить клиентский контейнер И его данные (список баз)? Лицензии сохранятся. [y/N] " ans
[ "$ans" = "y" ] || exit 0

$DOCKER compose down -v
echo "Готово."
