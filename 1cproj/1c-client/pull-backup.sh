#!/bin/bash
. "$(dirname "$0")/common.sh"

usage() {
    cat <<USAGE
Дамп базы на сервере 1С и копирование его на этот компьютер (каталог ./export).

  ./pull-backup.sh [--move] [база] [пользователь]     выгрузка в .dt (по умолчанию)
  ./pull-backup.sh [--move] --pg [база]               дамп PostgreSQL (.dump)

  --move   после копирования удалить файл на сервере

Выгрузку выполняет ./backup.sh на сервере, файл забирается по SSH. Пароль пользователя 1С и пароль sudo
скрипт спросит интерактивно (ssh -t). В .env должны быть заданы SERVER_SSH и SERVER_PROJECT_DIR
(абсолютный путь к 1c-server на сервере). Лучше настроить вход по ключу (ssh-copy-id).
USAGE
}

MOVE=0; MODE=dt
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        --move) MOVE=1; shift ;;
        --pg) MODE=pg; shift ;;
        *) break ;;
    esac
done
IB="${1:-${IB_NAME:-mybase}}"
[[ "$IB" =~ ^[A-Za-z0-9_.-]{1,64}$ && "$IB" != "." && "$IB" != ".." ]] || die "недопустимое имя базы"
IB_USER="$2"

[ -n "$SERVER_SSH" ] || die "не задан SERVER_SSH в .env (например user@192.168.1.10)"
[ -n "$SERVER_PROJECT_DIR" ] || die "не задан SERVER_PROJECT_DIR в .env (полный путь к 1c-server на сервере)"
case "$SERVER_PROJECT_DIR" in /*) ;; *) die "SERVER_PROJECT_DIR должен быть абсолютным путём (начинаться с /)";; esac

RSSH() { ssh $SERVER_SSH_OPTS "$@"; }
RDIR=$(printf '%q' "$SERVER_PROJECT_DIR")
QIB=$(printf '%q' "$IB")

if [ "$MODE" = dt ]; then
    EXT=dt
    REMOTE_BACKUP_DIR="$RDIR/backups/$QIB/dt"
    DEST_DIR="$EXPORT_DIR/$IB/dt"
    REMOTE_CMD="cd $RDIR && ./backup.sh $QIB"
    [ -n "$IB_USER" ] && REMOTE_CMD="$REMOTE_CMD $(printf '%q' "$IB_USER")"
else
    EXT=dump
    REMOTE_BACKUP_DIR="$RDIR/backups/$QIB/pg"
    DEST_DIR="$EXPORT_DIR/$IB/pg"
    REMOTE_CMD="cd $RDIR && ./backup.sh --pg $QIB"
fi

echo "== Выгрузка на сервере ($SERVER_SSH)"
RSSH -t "$SERVER_SSH" "$REMOTE_CMD" || die "выгрузка на сервере не удалась (см. сообщения выше)"

echo "== Поиск файла"
REMOTE_FILE=$(RSSH "$SERVER_SSH" "ls -t $REMOTE_BACKUP_DIR/${QIB}_*.${EXT} 2>/dev/null | head -n1")
[ -n "$REMOTE_FILE" ] || die "на сервере не найден файл backups/${IB}_*.${EXT}"
NAME=$(basename "$REMOTE_FILE")
RQ=$(printf '%q' "$REMOTE_FILE")

mkdir -p "$DEST_DIR" "$EXPORT_DIR/$IB/logs"
echo "== Копирование $NAME в $DEST_DIR"
RSIZE=$(RSSH "$SERVER_SSH" "wc -c < $RQ" | tr -d '[:space:]')
RSSH "$SERVER_SSH" "cat $RQ" > "$DEST_DIR/${NAME}.part" || { rm -f "$DEST_DIR/${NAME}.part"; die "копирование не удалось"; }
LSIZE=$(wc -c < "$DEST_DIR/${NAME}.part" | tr -d '[:space:]')
if [ "$RSIZE" != "$LSIZE" ]; then
    rm -f "$DEST_DIR/${NAME}.part"
    die "размеры не совпали (на сервере $RSIZE, получено $LSIZE байт)"
fi
mv "$DEST_DIR/${NAME}.part" "$DEST_DIR/${NAME}"

if [ "$MOVE" = 1 ]; then
    RSSH "$SERVER_SSH" "rm -f $RQ" && echo "Файл на сервере удалён."
fi

ls -lh "$DEST_DIR/${NAME}"
echo "Готово: $(pwd)/$DEST_DIR/${NAME}"
