#!/bin/bash
# Установка платформы 1С из каталога dist (.deb или установщик .run).
# Переменные: ONEC_VERSION (обязательна), ONEC_RUN_COMPONENTS (компоненты для setup-full*.run),
#   ONEC_REQUIRED_BIN (бинарники, без которых установка считается неудачной, через пробел),
#   ONEC_OPTIONAL_BIN (только предупреждение), DIST, OPT_DIR, STUB_DIR (для тестов).
set -eu

DIST="${DIST:-/tmp/dist}"
OPT_DIR="${OPT_DIR:-/opt/1cv8}"
STUB_DIR="${STUB_DIR:-/usr/local/bin}"
: "${ONEC_VERSION:?не задан ONEC_VERSION}"
COMPONENTS="${ONEC_RUN_COMPONENTS:-}"
REQUIRED_BIN="${ONEC_REQUIRED_BIN:-}"
OPTIONAL_BIN="${ONEC_OPTIONAL_BIN:-}"

die() { echo "ОШИБКА: $*" >&2; exit 1; }

ARCH=$(dpkg --print-architecture)
case "$ARCH" in amd64) RARCH=x86_64;; arm64) RARCH=aarch64;; *) RARCH="$ARCH";; esac
# Версия в пакетах 1С встречается в двух видах: 8.3.24.1467 и 8.3.24-1467.
# Принимаем оба, чтобы фильтр не отбрасывал все пакеты.
PV=$(echo "$ONEC_VERSION" | sed 's/\.\([0-9]*\)$/-\1/')
ver_ok() { [ "$1" = "$ONEC_VERSION" ] || [ "$1" = "$PV" ]; }
echo "Архитектура: $ARCH ($RARCH); версия: $ONEC_VERSION (в пакетах $ONEC_VERSION или $PV)"
ls -la "$DIST" || die "нет каталога $DIST"

WORK=$(mktemp -d); mkdir -p "$WORK/pkgs"
trap 'rm -rf "$WORK"' EXIT

# --- .deb: только своя архитектура и версия ---
for f in "$DIST"/*.deb; do
    [ -f "$f" ] || continue
    A=$(dpkg-deb -f "$f" Architecture); V=$(dpkg-deb -f "$f" Version)
    if { [ "$A" = "$ARCH" ] || [ "$A" = "all" ]; } && ver_ok "$V"; then
        cp "$f" "$WORK/pkgs/"
    else
        echo "Пропускаю $(basename "$f") (архитектура $A, версия $V)"
    fi
done

# --- .run: setup-full предпочтительнее setup-thin; нужны версия и архитектура в имени ---
RUNFILE=""
for r in "$DIST"/setup-full*.run "$DIST"/*.run; do
    [ -f "$r" ] || continue
    n=$(basename "$r")
    case "$n" in *"$ONEC_VERSION"*) ;; *) echo "Пропускаю $n (версия не $ONEC_VERSION)"; continue;; esac
    case "$n" in *"$RARCH"*|*"$ARCH"*) RUNFILE="$r"; break;; *) echo "Пропускаю $n (архитектура не $RARCH)";; esac
done

# Установщики и postinst рассчитаны на systemd, которого в контейнере нет
if ! command -v systemctl >/dev/null 2>&1; then
    mkdir -p "$STUB_DIR"
    printf '#!/bin/sh\nexit 0\n' > "$STUB_DIR/systemctl"; chmod +x "$STUB_DIR/systemctl"
fi

find_bin_dir() { find "$OPT_DIR" -maxdepth 2 -type d -name "$ONEC_VERSION" 2>/dev/null | head -n1; }

missing_required() {
    local d b; d=$(find_bin_dir); MISSING=""
    [ -n "$d" ] || { MISSING="(каталог платформы не найден)"; return 0; }
    for b in $REQUIRED_BIN; do [ -x "$d/$b" ] || MISSING="$MISSING $b"; done
}

if ls "$WORK"/pkgs/*.deb >/dev/null 2>&1; then
    echo "== Установка из .deb =="
    ls -la "$WORK/pkgs"
    apt-get update
    if [ -f "$DIST/v8-install-deps.sh" ]; then bash "$DIST/v8-install-deps.sh" || true; fi
    apt-get install -y "$WORK"/pkgs/*.deb
elif [ -n "$RUNFILE" ]; then
    echo "== Установка из $(basename "$RUNFILE") =="
    chmod +x "$RUNFILE"
    if [ -f "$DIST/v8-install-deps.sh" ]; then (apt-get update; bash "$DIST/v8-install-deps.sh") || true; fi

    # Перед запуском фиксируем состояние временной ФС и раздела установки.
    echo "== Диагностика места перед установкой =="
    df -hT /tmp "$OPT_DIR" 2>&1 || true
    df -i /tmp "$OPT_DIR" 2>&1 || true
    echo "Параметры установщика: компоненты=${COMPONENTS:-(по умолчанию)}"

    # Не повторяем установку с другими компонентами после ошибки: setup-full
    # может оставить частично распакованное дерево, после чего повторный запуск
    # маскирует первичную причину сообщением payload initialization failed.
    INSTALL_TRACE="/tmp/onec-installer-debugtrace.log"
    rm -f "$INSTALL_TRACE"
    INSTALL_ARGS=()
    case "$(basename "$RUNFILE")" in
        setup-thin*) ;; # setup-thin не принимает серверные компоненты
        *)
            if [ -n "$COMPONENTS" ]; then
                allowed=$("$RUNFILE" --help 2>&1 | awk '
                    /--enable-components/ {f=1; next}
                    f && /(Разрешено|Allowed)/ {sub(/^.*(Разрешено|Allowed)[^:]*:[ ]*/, ""); print; exit}
                    f && /^[ ]*--/ {exit}') || true
                echo "Допустимые значения --enable-components: ${allowed:-(определить не удалось)}"
                allowed_norm=$(echo "$allowed" | tr ',' ' ')
                filtered=""
                for c in ${COMPONENTS//,/ }; do
                    case " $allowed_norm " in
                        *" $c "*) filtered="${filtered:+$filtered,}$c" ;;
                        *) echo "ПРЕДУПРЕЖДЕНИЕ: компонент '$c' отсутствует в списке установщика и будет пропущен" ;;
                    esac
                done
                [ -z "$filtered" ] || INSTALL_ARGS+=(--enable-components "$filtered")
            fi ;;
    esac

    echo ">>> $(basename "$RUNFILE") --mode unattended --debuglevel 4 --debugtrace $INSTALL_TRACE ${INSTALL_ARGS[*]:-(компоненты по умолчанию)}"
    if "$RUNFILE" --mode unattended --debuglevel 4 --debugtrace "$INSTALL_TRACE" "${INSTALL_ARGS[@]}"; then
        missing_required
        if [ -n "$MISSING" ]; then
            echo "Установщик завершился с кодом 0, но отсутствуют обязательные бинарники:$MISSING"
            d=$(find_bin_dir)
            [ -z "$d" ] || { echo "Содержимое $d:"; ls -la "$d" | head -60; }
            [ ! -f "$INSTALL_TRACE" ] || { echo "Последние строки журнала установщика:"; tail -n 120 "$INSTALL_TRACE"; }
            die "установка из $(basename "$RUNFILE") неполная; повторный запуск автоматически не выполняется"
        fi
    else
        rc=$?
        echo "Установщик завершился с кодом $rc"
        echo "== Состояние файловой системы после ошибки =="
        df -hT /tmp "$OPT_DIR" 2>&1 || true
        df -i /tmp "$OPT_DIR" 2>&1 || true
        d=$(find_bin_dir)
        [ -z "$d" ] || { echo "Содержимое $d:"; ls -la "$d" | head -60; }
        if [ -f "$INSTALL_TRACE" ]; then
            echo "== Последние 160 строк debugtrace =="
            tail -n 160 "$INSTALL_TRACE"
        else
            echo "Установщик не создал debugtrace: $INSTALL_TRACE"
        fi
        die "установка из $(basename "$RUNFILE") завершилась ошибкой; повторная попытка отключена, см. debugtrace выше"
    fi
else
    die "в $DIST нет .deb или .run для архитектуры $ARCH ($RARCH) и версии $ONEC_VERSION"
fi

BIN=$(find_bin_dir)
[ -n "$BIN" ] || { ls -R "$OPT_DIR" 2>/dev/null | head -30; die "каталог платформы $ONEC_VERSION не найден в $OPT_DIR"; }
missing_required
[ -z "$MISSING" ] || { ls -la "$BIN" | head -40; die "после установки нет:$MISSING (каталог $BIN)"; }
for b in $OPTIONAL_BIN; do [ -x "$BIN/$b" ] || echo "ВНИМАНИЕ: нет $b (часть функций недоступна)"; done
ln -sfn "$BIN" "$OPT_DIR/current"
echo "Платформа: $BIN -> $OPT_DIR/current"
ls -la "$OPT_DIR/current/" | head -50
