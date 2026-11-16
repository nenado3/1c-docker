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

    attempts=()
    case "$(basename "$RUNFILE")" in
        setup-thin*) ;;   # тонкий установщик сам ставит клиент; его --enable-components принимает только языки
        *)
            if [ -n "$COMPONENTS" ]; then
                attempts+=("--enable-components $COMPONENTS")
                # допустимые значения берём из справки установщика и оставляем только их
                allowed=$("$RUNFILE" --help 2>&1 | awk '
                    /--enable-components/ {f=1; next}
                    f && /(Разрешено|Allowed)/ {sub(/^.*(Разрешено|Allowed)[^:]*:[ ]*/, ""); print; exit}
                    f && /^[ ]*--/ {exit}') || true
                echo "Допустимые значения --enable-components: ${allowed:-(определить не удалось)}"
                # в справке список через запятую, в COMPONENTS — тоже; сравниваем нормализованные наборы
                allowed_norm=$(echo "$allowed" | tr ',' ' ')
                filtered=""; nonlang=0
                for c in ${COMPONENTS//,/ }; do
                    case " $allowed_norm " in *" $c "*)
                        filtered="${filtered:+$filtered,}$c"
                        case " az ar hy bg hu el vi ka kk zh it es lv lt de pl ro ru tr tk fr uk en langs " in
                            *" $c "*) ;; *) nonlang=1;; esac ;;
                    esac
                done
                if [ -n "$filtered" ] && [ "$nonlang" = 1 ] && [ "$filtered" != "$COMPONENTS" ]; then
                    attempts+=("--enable-components $filtered")
                fi
            fi ;;
    esac
    attempts+=("")   # последняя попытка: компоненты по умолчанию

    done_ok=0
    for a in "${attempts[@]}"; do
        echo ">>> $(basename "$RUNFILE") --mode unattended ${a:-(компоненты по умолчанию)}"
        # shellcheck disable=SC2086
        if "$RUNFILE" --mode unattended $a; then
            missing_required
            if [ -z "$MISSING" ]; then done_ok=1; break; fi
            echo "Установщик отработал, но не хватает:$MISSING. Пробую следующий вариант компонентов."
        else
            echo "Установщик вернул ошибку с этими параметрами."
        fi
    done
    if [ "$done_ok" = 0 ]; then
        echo "Справка установщика:"; "$RUNFILE" --help 2>&1 || true
        d=$(find_bin_dir); [ -n "$d" ] && { echo "Содержимое $d:"; ls -la "$d" | head -40; }
        die "установка из $(basename "$RUNFILE") не удалась (нет:${MISSING:- ?}). Задайте допустимые компоненты в ONEC_RUN_COMPONENTS (.env), список выше"
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
