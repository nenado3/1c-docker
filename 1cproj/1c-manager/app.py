#!/usr/bin/env python3
"""Менеджер контейнеров 1С: локальная веб-панель (только стандартная библиотека Python).
Слушает только 127.0.0.1, требует случайный токен, выполняет лишь заранее заданные действия."""
import argparse, json, os, re, secrets, shutil, subprocess, sys, threading, time, webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote, urlencode
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = {}
jobs, jobs_lock = {}, threading.Lock()
CONTAINERS = {"onec-server", "onec-postgres", "onec-client"}
NAME_RE = re.compile(r"^(?!\.{1,2}$)[A-Za-z0-9_.-]{1,64}$")
UUID_RE = re.compile(r"^[0-9a-fA-F-]{36}$")
MEM_RE = re.compile(r"^(0|[0-9]{1,6}[bkmg]?)$")

RAC_PRE = ('R=/opt/1cv8/current/rac; '
           "C=$($R cluster list 2>&1 | awk '/^cluster/{print $3; exit}'); "
           '[ -n "$C" ] || { echo "кластер 1С не найден (сервер запущен? RAS на 1545?)" >&2; exit 3; }; ')
RAC = {
    "infobases": RAC_PRE + '$R infobase summary list --cluster=$C',
    "sessions": RAC_PRE + '$R session list --cluster=$C',
    "create": RAC_PRE + ('$R infobase create --cluster=$C --name="$1" --dbms=PostgreSQL --db-server=postgres '
                         '--db-name="$2" --db-user="$3" --db-pwd="$4" --locale=ru --create-database'),
    "drop": RAC_PRE + '$R infobase drop --cluster=$C --infobase="$1" --infobase-user="$2" --infobase-pwd="$3" ${4:+"$4"}',
    "kill": RAC_PRE + '$R session terminate --cluster=$C --session="$1"',
}


# ---------------- docker ----------------
def detect_docker():
    for cmd in (["docker"], ["sudo", "-n", "docker"]):
        try:
            if subprocess.run(cmd + ["info"], capture_output=True, timeout=20).returncode == 0:
                return cmd
        except Exception:
            pass
    return None


def dk(*args, timeout=40):
    if not cfg["docker"]:
        raise RuntimeError("Docker недоступен: добавьте пользователя в группу docker "
                           "(sudo usermod -aG docker $USER, затем перезайдите) или запустите панель от root")
    return subprocess.run(cfg["docker"] + list(args), capture_output=True, text=True,
                          errors="replace", timeout=timeout)


def parse_rac(text):
    items, cur = [], {}
    for line in text.splitlines():
        if not line.strip():
            if cur:
                items.append(cur); cur = {}
            continue
        if ":" in line:
            k, v = line.split(":", 1)
            cur[k.strip()] = v.strip().strip('"')
    if cur:
        items.append(cur)
    return items


def read_env(d):
    env, p = {}, os.path.join(d, ".env")
    if os.path.isfile(p):
        for line in open(p, encoding="utf-8", errors="replace"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1); v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                v = v[1:-1]
            env[k.strip()] = v
    return env


def write_env_value(path, key, value):
    """Записывает KEY=value в .env, сохраняя остальные строки и комментарии."""
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.strip().startswith(key + "="):
            lines[i] = "%s=%s" % (key, value)
            found = True
            break
    if not found:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("%s=%s" % (key, value))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")



# Настройки .env, доступные из веб-панели. Неизвестные ключи не принимаются.
ENV_SETTINGS = {
    "server": {
        "ONEC_VERSION": "Версия платформы 1С", "POSTGRES_USER": "Пользователь PostgreSQL",
        "POSTGRES_PASSWORD": "Пароль PostgreSQL", "IB_NAME": "Имя информационной базы по умолчанию",
        "DB_NAME": "Имя базы PostgreSQL по умолчанию", "DB_USER": "Пользователь БД по умолчанию",
        "SERVER_BASE_IMAGE": "Базовый образ сервера", "POSTGRES_BASE_IMAGE": "Базовый образ PostgreSQL",
        "BACKUP_DIR": "Каталог резервных копий", "SERVER_MEMORY_LIMIT": "Лимит памяти сервера",
        "POSTGRES_MEMORY_LIMIT": "Лимит памяти PostgreSQL"
    },
    "client": {
        "ONEC_VERSION": "Версия платформы 1С", "ONEC_RUN_COMPONENTS": "Компоненты установщика",
        "CLIENT_UID": "UID пользователя клиента", "SERVER_IP": "IP-адрес сервера 1С",
        "NETHASP_ADDRS": "Адреса NetHASP", "EXPORT_DIR": "Каталог выгрузок",
        "CLIENT_MEMORY_LIMIT": "Лимит памяти клиентского контейнера"
    }
}
ENV_SECRET_KEYS = {"POSTGRES_PASSWORD"}


def settings_snapshot():
    result = {}
    for section, keys in ENV_SETTINGS.items():
        directory = cfg["server_dir"] if section == "server" else cfg["client_dir"]
        values = read_env(directory)
        result[section] = {key: {"label": label, "value": "" if key in ENV_SECRET_KEYS else values.get(key, ""),
                                "configured": bool(values.get(key, "")) if key in ENV_SECRET_KEYS else None,
                                "secret": key in ENV_SECRET_KEYS}
                           for key, label in keys.items()}
    return result


def save_settings(body):
    changed = []
    old_backup_dir = os.path.realpath(data_dir("backups"))
    for section, keys in ENV_SETTINGS.items():
        values = body.get(section, {})
        if not isinstance(values, dict):
            raise ValueError("неверный формат настроек: " + section)
        directory = cfg["server_dir"] if section == "server" else cfg["client_dir"]
        if not os.path.isdir(directory):
            raise ValueError("каталог проекта не найден: " + directory)
        path = os.path.join(directory, ".env")
        for key, raw in values.items():
            if key not in keys:
                raise ValueError("недопустимый параметр .env: " + str(key))
            value = str(raw)
            if key in ENV_SECRET_KEYS and value == "":
                continue
            if key == "BACKUP_DIR":
                value = os.path.expanduser(value.strip())
                if not value:
                    raise ValueError("укажите каталог резервных копий")
                if not os.path.isabs(value):
                    value = os.path.abspath(os.path.join(directory, value))
                if not os.path.isdir(value):
                    raise ValueError("каталог резервных копий не существует или недоступен: " + value)
            if len(value) > 500 or any(ord(ch) < 32 for ch in value):
                raise ValueError("недопустимое значение параметра " + key)
            if any(ch in value for ch in ('"', "'", "\\", "#", "$")):
                raise ValueError("параметр %s содержит символы, несовместимые с форматом .env: кавычки, #, $ и обратная косая черта не поддерживаются" % key)
            if key in ("SERVER_MEMORY_LIMIT", "POSTGRES_MEMORY_LIMIT", "CLIENT_MEMORY_LIMIT") and not MEM_RE.match(value.lower()):
                raise ValueError("%s: укажите 0 или значение, например 8g или 512m" % key)
            if key == "CLIENT_UID" and value and not value.isdigit():
                raise ValueError("CLIENT_UID должен быть числом")
            if key == "ONEC_VERSION" and value and not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+", value):
                raise ValueError("ONEC_VERSION должен иметь формат 8.5.1.1522")
            write_env_value(path, key, value)
            changed.append(section + ":" + key)
    new_backup_dir = os.path.realpath(data_dir("backups"))
    return {"saved": changed, "backup_dir": new_backup_dir,
            "backup_dir_changed": old_backup_dir != new_backup_dir}


def data_dir(kind):
    if kind == "backups":
        # Должен совпадать с BACKUP_DIR в docker-compose.yml и common.sh:
        # этот каталог используется и веб-загрузкой, и контейнером onec-server.
        base, key, default = cfg["server_dir"], "BACKUP_DIR", "./backups"
    elif kind == "export":
        base, key, default = cfg["client_dir"], "EXPORT_DIR", "./export"
    else:
        base, key, default = cfg["server_dir"], None, "./licenses"
    val = read_env(base).get(key, default) if key else default
    return os.path.normpath(os.path.join(base, os.path.expanduser(val)))


def backup_base_dir(base_name, kind=None):
    """Каталог конкретной информационной базы и его подкаталоги."""
    name = str(base_name or "").strip()
    if not NAME_RE.fullmatch(name):
        raise ValueError("недопустимое имя информационной базы")
    root = os.path.join(data_dir("backups"), name)
    # Каждая ИБ всегда получает полный набор каталогов, даже если пока
    # выполняется только одна операция (например, загрузка .cf).
    os.makedirs(root, exist_ok=True)
    for folder in ("dt", "cf", "pg", "logs"):
        os.makedirs(os.path.join(root, folder), exist_ok=True)
    if kind:
        if kind not in ("dt", "cf", "pg", "logs"):
            raise ValueError("недопустимый тип каталога резервной копии")
        root = os.path.join(root, kind)
    return root


def walk_files(d, rel=""):
    """Обход каталога со вложенными папками (для веб-обзора backup/export)."""
    out = []
    try:
        entries = sorted(os.scandir(d), key=lambda e: (not e.is_dir(follow_symlinks=False), e.name.lower()))
    except OSError:
        return out
    dirs, files = [], []
    for e in entries:
        if e.name.startswith(".") or e.is_symlink():
            continue
        r = (rel + "/" + e.name) if rel else e.name
        if e.is_dir(follow_symlinks=False):
            dirs.append({"name": e.name, "path": r, "type": "dir"})
            dirs.extend(walk_files(e.path, r))
        elif e.is_file():
            try:
                files.append({"name": e.name, "path": r, "type": "file",
                              "size": e.stat().st_size, "mtime": e.stat().st_mtime})
            except OSError:
                pass
    return dirs + files


def list_files(kind):
    d, out = data_dir(kind), []
    if os.path.isdir(d):
        for n in os.listdir(d):
            p = os.path.join(d, n)
            if os.path.isfile(p) and not n.startswith("."):
                out.append({"name": n, "size": os.path.getsize(p), "mtime": os.path.getmtime(p)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return {"dir": d, "files": out, "tree": walk_files(d)}


# ---------------- фоновые задачи ----------------
class Job:
    def __init__(self, title):
        self.id, self.title = secrets.token_hex(6), title
        self.lines, self.status, self.rc, self.started = [], "running", None, time.time()


def run_job(title, argv, cwd=None):
    job = Job(title)
    with jobs_lock:
        jobs[job.id] = job
        if len(jobs) > 30:
            for k in sorted(jobs, key=lambda k: jobs[k].started)[:len(jobs) - 30]:
                del jobs[k]
    env = os.environ.copy()
    env["DOCKER"] = " ".join(cfg["docker"] or ["docker"])

    def worker():
        try:
            p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
            for line in p.stdout:
                job.lines.append(line.rstrip("\n"))
            job.rc = p.wait()
        except Exception as e:
            job.lines.append("Не удалось запустить: %s" % e); job.rc = -1
        job.status = "done" if job.rc == 0 else "error"

    threading.Thread(target=worker, daemon=True).start()
    return job.id


def project(name):
    d = cfg["server_dir"] if name == "server" else cfg["client_dir"]
    if not os.path.isdir(d):
        raise ValueError("папка проекта не найдена: %s" % d)
    return d


def script_job(title, proj, script, *args):
    d = project(proj)
    path = os.path.join(d, script)
    if not os.path.isfile(path):
        raise ValueError("нет скрипта %s" % path)
    return run_job(title, ["bash", path] + [str(a) for a in args], cwd=d)


def rac_job(title, key, *args):
    if not cfg["docker"]:
        raise ValueError("Docker недоступен")
    return run_job(title, cfg["docker"] + ["exec", "onec-server", "bash", "-c", RAC[key], "_"] + list(args))


# ---------------- действия ----------------
def need(body, key, rx=None, maxlen=200):
    v = str(body.get(key, "")).strip()
    if not v or len(v) > maxlen or any(ord(c) < 32 for c in v):
        raise ValueError("не задано или некорректно поле: %s" % key)
    if rx and not rx.match(v):
        raise ValueError("недопустимые символы в поле: %s" % key)
    return v


def do_action(body):
    a = body.get("action")
    if a == "container_op":
        name, op = need(body, "name"), need(body, "op")
        if name not in CONTAINERS or op not in ("start", "stop", "restart"):
            raise ValueError("недопустимое действие")
        return run_job("%s %s" % (op, name), cfg["docker"] + [op, name])
    if a == "compose_op":
        proj, op = need(body, "project"), need(body, "op")
        if proj not in ("server", "client") or op not in ("up", "stop", "restart"):
            raise ValueError("недопустимое действие")
        d = project(proj)
        # клиент и запуск сервера идут через скрипты проектов: common.sh переводит имя хоста SERVER_IP в IP,
        # генерирует nethasp.ini и создаёт каталоги (прямой docker compose всё это пропускает)
        if proj == "client":
            sub = {"up": "up", "stop": "stop", "restart": "recreate"}[op]
            return script_job("Клиентский контейнер: %s" % op, "client", "client.sh", sub)
        if op == "up":
            return script_job("Запуск сервера (postgres + 1С)", "server", "start.sh")
        return run_job("Сервер: %s" % op, cfg["docker"] + ["compose", op], cwd=d)
    if a == "build":
        proj = need(body, "project")
        if proj not in ("server", "client"):
            raise ValueError("недопустимый проект")
        args = []
        if proj == "server":
            # необязательные папки с установщиками: пустая строка — использовать server/dist и postgres/dist
            for key, label in (("server_dist", "установщики 1С"), ("pg_dist", "пакеты PostgreSQL")):
                v = str(body.get(key, "")).strip()
                if v:
                    if len(v) > 400 or not os.path.isabs(v) or not os.path.isdir(v):
                        raise ValueError("папка для %s не найдена: %s" % (label, v))
                args.append(v)
            # Лимиты контейнеров сохраняются в .env и применяются при запуске/пересоздании.
            limits = (
                ("memory", "SERVER_MEMORY_LIMIT", project("server")),
                ("postgres_memory", "POSTGRES_MEMORY_LIMIT", project("server")),
                ("client_memory", "CLIENT_MEMORY_LIMIT", project("client")),
            )
            for field, env_key, directory in limits:
                value = str(body.get(field, "")).strip().lower()
                if value:
                    if not MEM_RE.match(value):
                        raise ValueError("%s: укажите 0 или значение, например 8g, 512m" % env_key)
                    write_env_value(os.path.join(directory, ".env"), env_key, value)
        return script_job("Сборка образов (%s)" % proj, proj, "build.sh", *args)
    if a == "update_image":
        proj = need(body, "project")
        if proj not in ("server", "client"):
            raise ValueError("недопустимый проект")
        return script_job("Обновление Docker-образа (%s)" % proj, proj, "update-image.sh")
    if a == "recreate_server":
        d = project("server")
        return run_job("Пересоздание контейнера onec-server с новыми настройками",
                       cfg["docker"] + ["compose", "up", "-d", "--force-recreate", "onec-server"], cwd=d)
    if a == "set_memory":
        val = need(body, "limit").lower()
        if not MEM_RE.match(val):
            raise ValueError("лимит памяти: 0 (без лимита) или число с суффиксом, например 8g, 512m")
        d = project("server")
        write_env_value(os.path.join(d, ".env"), "SERVER_MEMORY_LIMIT", val)
        title = "Память сервера 1С: без лимита" if val == "0" else "Память сервера 1С: %s" % val
        return run_job(title, cfg["docker"] + ["compose", "up", "-d", "onec-server"], cwd=d)
    if a == "start_server":
        return script_job("Запуск сервера и создание базы", "server", "start.sh")
    if a == "start_server_with_dt":
        ib = need(body, "ib", NAME_RE)
        filename = need(body, "filename", re.compile(r"^[A-Za-z0-9_.-]{1,180}\.dt$", re.I), 190)
        db_name = str(body.get("db_name") or ib).strip()
        db_user = str(body.get("db_user") or "").strip()
        db_pwd = str(body.get("db_pwd") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", db_name):
            raise ValueError("имя базы PostgreSQL должно содержать 1–63 допустимых символа")
        if db_user and not NAME_RE.match(db_user):
            raise ValueError("недопустимое имя пользователя PostgreSQL")
        if bool(db_user) != bool(db_pwd):
            raise ValueError("для отдельного пользователя PostgreSQL укажите и логин, и пароль; иначе оставьте оба поля пустыми для использования .env")
        if len(db_pwd) > 200:
            raise ValueError("слишком длинный пароль PostgreSQL")
        uploaded_file = os.path.join(backup_base_dir(ib, "dt"), filename)
        if not os.path.isfile(uploaded_file) or os.path.getsize(uploaded_file) <= 0:
            raise ValueError("Загруженный .dt не найден или пуст: %s" % uploaded_file)
        return script_job("Запуск сервера и создание базы %s из .dt" % ib, "server",
                          "start-with-dt.sh", ib, filename, uploaded_file, db_name, db_user, db_pwd)
    if a == "backup_dt":
        ib = need(body, "ib", NAME_RE)
        user, pwd = str(body.get("user", "")).strip(), str(body.get("pwd", ""))
        args = [ib] + ([user, pwd] if user else [])
        return script_job("Выгрузка .dt базы %s" % ib, "server", "backup.sh", *args)
    if a == "backup_cf":
        ib = need(body, "ib", NAME_RE)
        user, pwd = str(body.get("user", "")).strip(), str(body.get("pwd", ""))
        args = ["--cf", ib] + ([user, pwd] if user else [])
        return script_job("Выгрузка .cf конфигурации базы %s" % ib, "server", "backup.sh", *args)
    if a == "import_file":
        ib = need(body, "ib", NAME_RE)
        filename = need(body, "filename", re.compile(r"^[A-Za-z0-9_.-]{1,180}\.(dt|cf)$", re.I), 190)
        kind = filename.rsplit(".", 1)[1].lower()
        new_base = bool(body.get("new_base"))
        user, pwd = str(body.get("user", "")).strip(), str(body.get("pwd", ""))
        # реквизиты PostgreSQL можно задать в панели; пустые — берутся из .env
        db_name = str(body.get("db_name") or ib).strip()
        if new_base and not re.fullmatch(r"[A-Za-z0-9_.-]{1,63}", db_name):
            raise ValueError("имя базы PostgreSQL должно содержать 1–63 допустимых символа")
        db_user = str(body.get("db_user", "")).strip()
        db_pwd = str(body.get("db_pwd", ""))
        if db_user and not NAME_RE.match(db_user):
            raise ValueError("недопустимое имя пользователя PostgreSQL")
        if new_base and bool(db_user) != bool(db_pwd):
            raise ValueError("для отдельного пользователя PostgreSQL укажите и логин, и пароль; иначе оставьте оба поля пустыми для использования .env")
        if len(db_pwd) > 200:
            raise ValueError("слишком длинный пароль PostgreSQL")
        # Передаём import.sh тот же абсолютный путь, куда API загрузки реально сохранил файл.
        # Это исключает расхождение BACKUP_DIR/cwd между веб-панелью и shell-скриптом.
        uploaded_file = os.path.join(backup_base_dir(ib, kind), filename)
        if not os.path.isfile(uploaded_file):
            raise ValueError("Загрузка не найдена на диске: %s. Проверьте ответ загрузки и каталог BACKUP_DIR." % uploaded_file)
        if os.path.getsize(uploaded_file) <= 0:
            raise ValueError("Загруженный файл пустой: %s" % uploaded_file)
        if new_base and kind != "dt":
            raise ValueError("создание новой базы поддерживается только из файла .dt")
        if kind == "cf":
            # Защита на стороне сервера: не начинаем обновление конфигурации,
            # пока в выбранной информационной базе остаются пользовательские сеансы.
            clusters = parse_rac(rac_call("cluster", "list"))
            cluster = next((item.get("cluster") for item in clusters if item.get("cluster")), None)
            if not cluster:
                raise ValueError("кластер 1С не найден")
            bases = parse_rac(rac_call("infobase", "summary", "list", "--cluster=" + cluster))
            base_info = next((item for item in bases if (item.get("name") or item.get("Name")) == ib), None)
            base_id = (base_info or {}).get("infobase")
            if not base_id:
                raise ValueError("не удалось определить идентификатор выбранной информационной базы")
            sessions = parse_rac(rac_call("session", "list", "--cluster=" + cluster))
            active = [item for item in sessions if item.get("infobase") == base_id]
            if active:
                raise ValueError("обновление заблокировано: в базе %s остаётся активных сеансов: %d" % (ib, len(active)))
        # Девятый аргумент включает строгий режим: только новая ИБ и новая БД PostgreSQL.
        args = [ib, filename, kind, user, pwd, db_user, db_pwd, uploaded_file, "new" if new_base else "", db_name if new_base else ""]
        title = "Создание базы %s из .dt" % ib if new_base else "Импорт .%s в базу %s" % (kind, ib)
        return script_job(title, "server", "import.sh", *args)
    if a == "backup_pg":
        return script_job("Дамп PostgreSQL %s" % need(body, "db", NAME_RE), "server", "backup.sh", "--pg", need(body, "db", NAME_RE))
    if a == "postgres_drop_db":
        db = need(body, "db", NAME_RE)
        if db.lower() in ("postgres", "template0", "template1"):
            raise ValueError("системные базы PostgreSQL удалять нельзя")
        # Сначала завершаем подключения, затем отдельной командой выполняем DROP DATABASE
        # (эта команда не может выполняться внутри транзакции).
        identifier = '"' + db.replace('"', '""') + '"'
        terminate_sql = "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '%s' AND pid <> pg_backend_pid();" % db.replace("'", "''")
        return run_job("Удаление базы PostgreSQL %s" % db,
                       cfg["docker"] + ["exec", "onec-postgres", "runuser", "-u", "postgres", "--",
                                        "/usr/local/pgbin/psql", "-d", "postgres", "-v", "ON_ERROR_STOP=1",
                                        "-c", terminate_sql, "-c", "DROP DATABASE " + identifier])
    if a == "restore_pg_dump":
        db = need(body, "db", NAME_RE)
        filename = need(body, "filename", re.compile(r"^[A-Za-z0-9_.-]{1,180}\.dump$", re.I), 190)
        uploaded_file = os.path.join(backup_base_dir(db, "pg"), filename)
        if not os.path.isfile(uploaded_file) or os.path.getsize(uploaded_file) <= 0:
            raise ValueError("Загруженный .dump не найден или пуст")
        clean = "yes" if body.get("clean") else "no"
        title = "Восстановление PostgreSQL dump в %s%s" % (db, " с очисткой объектов" if clean == "yes" else "")
        return script_job(title, "server", "restore-pg.sh", db, uploaded_file, clean)
    if a == "client_start":
        return script_job("Запуск клиента 1С", "client", "client.sh")
    if a == "license_install":
        return script_job("Перенос лицензий", "client", "license.sh", "install")
    if a == "apache_op":
        op = need(body, "op")
        if op not in ("start", "stop", "restart", "status"):
            raise ValueError("недопустимое действие Apache")
        return run_job("Apache: " + op, cfg["docker"] + ["exec", "onec-server", "/usr/local/bin/manage-web.sh", op])
    if a in ("web_publish", "web_delete"):
        name = need(body, "name", NAME_RE)
        op = "publish" if a == "web_publish" else "delete"
        title = ("Публикация веб-клиента базы " if op == "publish" else "Удаление веб-публикации базы ") + name
        return run_job(title, cfg["docker"] + ["exec", "onec-server", "/usr/local/bin/manage-web.sh", op, name])
    if a == "license_server_activate":
        filename = need(body, "filename", re.compile(r"^[A-Za-z0-9_.-]{1,176}\.lic$", re.I), 180)
        license_file = os.path.join(data_dir("licenses"), filename)
        if not os.path.isfile(license_file) or os.path.getsize(license_file) <= 0:
            raise ValueError("Файл лицензии не найден или пуст: %s" % license_file)
        return script_job("Активация лицензии на сервере: %s" % filename, "server", "license.sh", "activate", filename)
    if a == "infobase_create":
        n = need(body, "name", NAME_RE)
        db = str(body.get("db_name") or n).strip()
        if not NAME_RE.match(db):
            raise ValueError("недопустимое имя базы PostgreSQL")
        # Подготавливаем каталоги базы заранее; выгрузки и журналы будут попадать сюда.
        for kind in ("dt", "cf", "logs"):
            backup_base_dir(n, kind)
        backup_base_dir(db, "pg")
        return rac_job("Создание базы %s" % n, "create", n, db, need(body, "db_user", NAME_RE), need(body, "db_pwd"))
    if a == "infobase_drop":
        i = need(body, "id", UUID_RE)
        ib_user = need(body, "ib_user", maxlen=128)
        ib_pwd = need(body, "ib_pwd", maxlen=256)
        return rac_job("Удаление базы %s" % i, "drop", i, ib_user, ib_pwd,
                       "--drop-database" if body.get("drop_db") else "")
    if a == "session_kill":
        return rac_job("Завершение сеанса", "kill", need(body, "id", UUID_RE))
    raise ValueError("неизвестное действие")


def migrate_legacy_backups():
    """Переносит старые файлы ИБ_дата.dt/.cf в backups/ИБ/тип."""
    root = data_dir("backups")
    for kind in ("dt", "cf"):
        legacy = os.path.join(root, kind)
        if not os.path.isdir(legacy):
            continue
        try:
            entries = list(os.scandir(legacy))
        except OSError:
            continue
        for entry in entries:
            if not entry.is_file(follow_symlinks=False):
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]{1,64})_\d{8}_\d{6}\." + kind, entry.name, re.I)
            if not match:
                continue
            target = os.path.join(backup_base_dir(match.group(1), kind), entry.name)
            if not os.path.exists(target):
                try:
                    shutil.move(entry.path, target)
                except OSError as exc:
                    print("Не удалось перенести старую копию %s: %s" % (entry.path, exc), file=sys.stderr)
        old_monthly = os.path.join(legacy, "monthly")
        if os.path.isdir(old_monthly):
            for entry in os.scandir(old_monthly):
                match = re.fullmatch(r"([A-Za-z0-9_.-]{1,64})_\d{8}_\d{6}\." + kind, entry.name, re.I)
                if not entry.is_file(follow_symlinks=False) or not match:
                    continue
                target_dir = os.path.join(backup_base_dir(match.group(1), kind), "monthly")
                os.makedirs(target_dir, exist_ok=True)
                target = os.path.join(target_dir, entry.name)
                if not os.path.exists(target):
                    try:
                        shutil.move(entry.path, target)
                    except OSError as exc:
                        print("Не удалось перенести месячную копию %s: %s" % (entry.path, exc), file=sys.stderr)
    # Старые дампы PostgreSQL лежали в корне backups или backups/pg.
    for legacy_pg in (root, os.path.join(root, "pg")):
        if not os.path.isdir(legacy_pg):
            continue
        for entry in os.scandir(legacy_pg):
            if not entry.is_file(follow_symlinks=False):
                continue
            match = re.fullmatch(r"([A-Za-z0-9_.-]{1,63})_\d{8}_\d{6}\.dump", entry.name, re.I)
            if not match:
                continue
            target_dir = backup_base_dir(match.group(1), "pg")
            target = os.path.join(target_dir, entry.name)
            if not os.path.exists(target):
                try:
                    shutil.move(entry.path, target)
                except OSError as exc:
                    print("Не удалось перенести старый дамп %s: %s" % (entry.path, exc), file=sys.stderr)


# ---------------- автоматические резервные копии ----------------
backup_schedule_lock = threading.Lock()
backup_schedule = {"enabled": False, "daily_time": "02:00", "monthly_enabled": False, "max_backups": 10, "last_run": None, "next_run": None, "monthly_next_run": None, "last_result": "Расписание не настроено"}
backup_schedule_path = None
backup_running = False


def save_backup_schedule():
    if not backup_schedule_path:
        return
    tmp = backup_schedule_path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(backup_schedule, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, backup_schedule_path)


def next_daily_backup(clock_time, now=None):
    """Следующий ежедневный запуск по локальному времени сервера."""
    now = now or datetime.now()
    hour, minute = map(int, clock_time.split(":"))
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate.timestamp()


def next_monthly_backup(now=None):
    """Первое число следующего месяца в 02:00 по локальному времени сервера."""
    now = now or datetime.now()
    if now.month == 12:
        first = datetime(now.year + 1, 1, 1, 2, 0, 0)
    else:
        first = datetime(now.year, now.month + 1, 1, 2, 0, 0)
    return first.timestamp()


def load_backup_schedule():
    global backup_schedule_path
    backup_schedule_path = os.path.join(cfg["server_dir"], ".backup-schedule.json")
    try:
        with open(backup_schedule_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            clock_time = data.get("daily_time", "02:00")
            maximum = int(data.get("max_backups", 10))
            if not isinstance(clock_time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock_time):
                clock_time = "02:00"
            if 1 <= maximum <= 1000:
                backup_schedule.update(data)
                backup_schedule["daily_time"] = clock_time
                backup_schedule["max_backups"] = maximum
                backup_schedule["monthly_enabled"] = data.get("monthly_enabled") is True
                backup_schedule["next_run"] = (backup_schedule.get("next_run") or
                    (next_daily_backup(clock_time) if backup_schedule.get("enabled") else None))
    except FileNotFoundError:
        pass
    except Exception as e:
        print("Не удалось прочитать расписание бэкапов: %s" % e, file=sys.stderr)


def backup_schedule_snapshot():
    with backup_schedule_lock:
        result = dict(backup_schedule)
        credentials = result.pop("credentials", {}) or {}
        result["credential_status"] = {
            name: {"username": item.get("username", ""), "has_password": bool(item.get("password"))}
            for name, item in credentials.items() if isinstance(item, dict)
        }
        result["running"] = backup_running
        return result


def save_backup_schedule_request(body):
    enabled = body.get("enabled") is True
    monthly_enabled = body.get("monthly_enabled") is True
    clock_time = body.get("daily_time", "02:00")
    if not isinstance(clock_time, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clock_time):
        raise ValueError("Укажите корректное ежедневное время в формате ЧЧ:ММ")
    try:
        maximum = int(body.get("max_backups", 10))
    except (TypeError, ValueError):
        raise ValueError("Максимальное количество копий должно быть числом")
    if maximum < 1 or maximum > 1000:
        raise ValueError("Максимальное количество обычных бэкапов должно быть от 1 до 1000")
    selected = body.get("selected_bases", [])
    if not isinstance(selected, list) or any(not isinstance(x, str) or not NAME_RE.fullmatch(x) for x in selected):
        raise ValueError("Некорректный список информационных баз")
    if len(selected) != len(set(selected)):
        raise ValueError("В списке баз есть повторения")
    if (enabled or monthly_enabled) and not selected:
        raise ValueError("Выберите хотя бы одну базу для автоматического резервного копирования")
    raw_credentials = body.get("credentials", {})
    if not isinstance(raw_credentials, dict):
        raise ValueError("Некорректные учётные данные баз")
    with backup_schedule_lock:
        credentials = dict(backup_schedule.get("credentials") or {})
        for base_name in selected:
            item = raw_credentials.get(base_name, {})
            if not isinstance(item, dict):
                raise ValueError("Некорректные учётные данные базы " + base_name)
            username = item.get("username", "")
            password = item.get("password", "")
            if not isinstance(username, str) or len(username) > 256 or not isinstance(password, str) or len(password) > 1024:
                raise ValueError("Логин или пароль слишком длинный")
            username = username.strip()
            if password:
                credentials[base_name] = {"username": username, "password": password}
            elif username:
                previous = credentials.get(base_name, {})
                credentials[base_name] = {"username": username, "password": previous.get("password", "")}
            elif base_name in credentials and not item.get("keep_existing", False):
                credentials.pop(base_name, None)
        backup_schedule["credentials"] = credentials
        backup_schedule["selected_bases"] = selected
        backup_schedule["enabled"] = enabled
        backup_schedule["daily_time"] = clock_time
        backup_schedule["monthly_enabled"] = monthly_enabled
        backup_schedule["max_backups"] = maximum
        backup_schedule["next_run"] = next_daily_backup(clock_time) if enabled else None
        backup_schedule["monthly_next_run"] = next_monthly_backup() if monthly_enabled else None
        backup_schedule["last_result"] = ("Расписание сохранено. " +
            ("Ежедневные копии — каждый день в %s. " % clock_time if enabled else "") +
            ("Ежемесячная копия — 1-го числа в 02:00. " if monthly_enabled else ""))
        if not enabled and not monthly_enabled:
            backup_schedule["last_result"] = "Автоматическое резервное копирование выключено"
        save_backup_schedule()
        result = dict(backup_schedule)
        result["running"] = backup_running
        return result

def prune_regular_backups(base_name, maximum):
    """Ограничивает число обычных .dt для базы; monthly/ не затрагивает."""
    directory = os.path.join(backup_base_dir(base_name), "dt")
    if not os.path.isdir(directory):
        return
    pattern = re.compile(r"^" + re.escape(base_name) + r"_\d{8}_\d{6}\.dt$")
    files = []
    for entry in os.scandir(directory):
        if entry.is_file(follow_symlinks=False) and pattern.fullmatch(entry.name):
            files.append((entry.stat().st_mtime, entry.path))
    files.sort(reverse=True)
    for _, path in files[maximum:]:
        try:
            os.remove(path)
            print("Удалена старая обычная копия: " + path, flush=True)
        except OSError as e:
            print("Не удалось удалить старую копию %s: %s" % (path, e), flush=True)


def move_to_monthly(base_name):
    """Переносит созданный месячный .dt в отдельный каталог, не участвующий в ротации."""
    directory = os.path.join(backup_base_dir(base_name), "dt")
    pattern = re.compile(r"^" + re.escape(base_name) + r"_\d{8}_\d{6}\.dt$")
    candidates = []
    if os.path.isdir(directory):
        for entry in os.scandir(directory):
            if entry.is_file(follow_symlinks=False) and pattern.fullmatch(entry.name):
                candidates.append((entry.stat().st_mtime, entry.path))
    if not candidates:
        raise RuntimeError("созданный файл месячной копии базы %s не найден" % base_name)
    _, source = max(candidates)
    target_dir = os.path.join(directory, "monthly")
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, os.path.basename(source))
    shutil.move(source, target)
    return target


def rac_call(*args, timeout=60):
    """Выполняет одну команду RAC внутри контейнера сервера 1С."""
    command = cfg["docker"] + ["exec", "onec-server", "/opt/1cv8/current/rac"] + list(args)
    result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=timeout)
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or
                           "ошибка RAC: " + " ".join(args[:2]))
    return result.stdout


def lock_infobase_for_backup(base_name, credential):
    """Запрещает новые сеансы и регламентные задания, завершает текущие сеансы."""
    username = (credential or {}).get("username", "").strip()
    password = (credential or {}).get("password", "")
    if not username or not password:
        raise RuntimeError("для автоматического отключения пользователей базы %s "
                           "укажите логин и пароль администратора 1С в настройках автобэкапа" % base_name)

    clusters = parse_rac(rac_call("cluster", "list"))
    cluster = next((x.get("cluster") for x in clusters if x.get("cluster")), None)
    if not cluster:
        raise RuntimeError("кластер 1С не найден")

    bases = parse_rac(rac_call("infobase", "summary", "list", "--cluster=" + cluster))
    info = next((x for x in bases if (x.get("name") or x.get("Name")) == base_name), None)
    infobase_id = (info or {}).get("infobase")
    if not infobase_id:
        raise RuntimeError("не найден идентификатор информационной базы " + base_name)

    permission_code = secrets.token_hex(8)
    common = ["--cluster=" + cluster, "--infobase=" + infobase_id]
    auth = ["--infobase-user=" + username, "--infobase-pwd=" + password]
    locked = False
    try:
        rac_call("infobase", "update", *common, *auth,
                 "--sessions-deny=on", "--scheduled-jobs-deny=on",
                 "--denied-message=Выполняется автоматическое резервное копирование",
                 "--permission-code=" + permission_code)
        locked = True
        print("[%s] Запрещены новые подключения и регламентные задания" % base_name, flush=True)

        for attempt in range(30):
            sessions = parse_rac(rac_call("session", "list", *common))
            ids = [item.get("session") for item in sessions if item.get("session")]
            if not ids:
                print("[%s] Все пользовательские сеансы завершены" % base_name, flush=True)
                # После завершения сеансов сразу разрешаем вход пользователей.
                # Регламентные задания остаются запрещены до конца выгрузки .dt.
                rac_call("infobase", "update", *common, *auth,
                         "--sessions-deny=off", "--scheduled-jobs-deny=on",
                         "--permission-code=" + permission_code)
                print("[%s] Новые подключения разрешены; начинается выгрузка .dt" % base_name, flush=True)
                return {"cluster": cluster, "infobase": infobase_id, "auth": auth,
                        "locked": True, "sessions_reenabled": True}
            for session_id in ids:
                rac_call("session", "terminate", "--cluster=" + cluster, "--session=" + session_id)
            print("[%s] Завершено сеансов: %d; ожидание отключения..." %
                  (base_name, len(ids)), flush=True)
            time.sleep(2)
        raise RuntimeError("не удалось завершить все сеансы базы %s за 60 секунд" % base_name)
    except Exception:
        if locked:
            try:
                rac_call("infobase", "update", *common, *auth,
                         "--sessions-deny=off", "--scheduled-jobs-deny=off")
                print("[%s] Блокировка снята после ошибки подготовки" % base_name, flush=True)
            except Exception as unlock_error:
                print("[%s] КРИТИЧЕСКИ: не удалось снять блокировку: %s" %
                      (base_name, unlock_error), flush=True)
        raise


def unlock_infobase_after_backup(base_name, lock_info):
    if not lock_info or not lock_info.get("locked"):
        return
    rac_call("infobase", "update",
             "--cluster=" + lock_info["cluster"],
             "--infobase=" + lock_info["infobase"],
             *lock_info["auth"], "--sessions-deny=off", "--scheduled-jobs-deny=off")
    print("[%s] Подключения и регламентные задания снова разрешены" % base_name, flush=True)


def automatic_backup_loop():
    global backup_running
    while True:
        time.sleep(15)
        now = time.time()
        with backup_schedule_lock:
            daily_due = bool(backup_schedule.get("enabled") and backup_schedule.get("next_run")
                             and now >= backup_schedule["next_run"])
            monthly_due = bool(backup_schedule.get("monthly_enabled") and backup_schedule.get("monthly_next_run")
                               and now >= backup_schedule["monthly_next_run"])
            if (not daily_due and not monthly_due) or backup_running:
                continue
            backup_running = True
            monthly_run = monthly_due
            if daily_due:
                backup_schedule["next_run"] = next_daily_backup(backup_schedule.get("daily_time", "02:00"))
            if monthly_due:
                backup_schedule["monthly_next_run"] = next_monthly_backup()
            maximum = int(backup_schedule.get("max_backups", 10))
            save_backup_schedule()
        result_lines = []
        try:
            if not cfg.get("docker"):
                raise RuntimeError("Docker недоступен")
            command = cfg["docker"] + ["exec", "onec-server", "bash", "-lc", RAC["infobases"]]
            result = subprocess.run(command, capture_output=True, text=True, errors="replace", timeout=60)
            if result.returncode:
                raise RuntimeError((result.stderr or result.stdout).strip() or "не удалось получить список баз 1С")
            available = []
            for item in parse_rac(result.stdout):
                name = item.get("name") or item.get("Name")
                if name and NAME_RE.fullmatch(name):
                    available.append(name)
            with backup_schedule_lock:
                requested_bases = list(backup_schedule.get("selected_bases") or [])
            bases = [name for name in requested_bases if name in available]
            if not bases:
                raise RuntimeError("Не выбраны доступные базы для резервного копирования")
            for name in bases:
                backup_env = os.environ.copy()
                backup_env["DOCKER"] = " ".join(cfg["docker"])
                with backup_schedule_lock:
                    credential = (backup_schedule.get("credentials") or {}).get(name, {})
                lock_info = lock_infobase_for_backup(name, credential)
                proc = None
                try:
                    proc = subprocess.run(["bash", os.path.join(cfg["server_dir"], "backup.sh"), name,
                                           credential.get("username", ""), credential.get("password", "")],
                                          cwd=cfg["server_dir"], env=backup_env, capture_output=True, text=True,
                                          errors="replace", timeout=6 * 3600)
                    result_lines.append("[%s] %s" % (name, "OK" if proc.returncode == 0 else "ОШИБКА"))
                    if proc.stdout:
                        result_lines.append(proc.stdout[-5000:])
                    if proc.stderr:
                        result_lines.append(proc.stderr[-2000:])
                    if proc.returncode:
                        raise RuntimeError("Ошибка резервного копирования базы %s" % name)
                finally:
                    unlock_infobase_after_backup(name, lock_info)
                if monthly_run:
                    monthly_path = move_to_monthly(name)
                    result_lines.append("[%s] Месячная копия сохранена: %s" % (name, monthly_path))
                else:
                    prune_regular_backups(name, maximum)
            summary = ("Ежемесячная копия создана: " if monthly_run else "Успешно: ") + ", ".join(bases)
        except Exception as e:
            summary = "Ошибка автоматического бэкапа: " + str(e)
        finally:
            with backup_schedule_lock:
                backup_schedule["last_run"] = time.time()
                backup_schedule["last_result"] = summary
                backup_running = False
                save_backup_schedule()
        print("\\n".join(result_lines[-30:]), flush=True)

def api_get(path, q):
    if path == "/api/settings":
        return settings_snapshot()
    if path == "/api/backup_schedule":
        return backup_schedule_snapshot()
    if path == "/api/backup_bases":
        r = dk("exec", "onec-server", "bash", "-lc", RAC["infobases"])
        if r.returncode:
            raise ValueError((r.stderr or r.stdout).strip() or "не удалось получить список баз")
        names = []
        for item in parse_rac(r.stdout):
            name = item.get("name") or item.get("Name")
            if name and NAME_RE.fullmatch(name):
                names.append(name)
                # Создаём структуру каталогов для каждой уже существующей ИБ.
                backup_base_dir(name)
        return {"items": sorted(set(names), key=str.lower)}
    if path == "/api/browse_dirs":
        requested = q.get("path", [os.path.expanduser("~")])[0]
        if len(requested) > 1000 or any(ord(ch) < 32 for ch in requested):
            raise ValueError("недопустимый путь к каталогу")
        current = os.path.abspath(os.path.expanduser(requested or "~"))
        if not os.path.isdir(current):
            raise ValueError("каталог не найден или недоступен: " + current)
        dirs = []
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name.lower())
            for entry in entries:
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append({"name": entry.name, "path": entry.path})
                except OSError:
                    continue
        except OSError as e:
            raise ValueError("не удалось прочитать каталог: " + str(e))
        parent = os.path.dirname(current) if current != os.path.dirname(current) else None
        return {"path": current, "parent": parent, "dirs": dirs}
    if path == "/api/status":
        return {"docker": bool(cfg["docker"]), "docker_cmd": " ".join(cfg["docker"] or []),
                "server_dir": cfg["server_dir"], "server_ok": os.path.isdir(cfg["server_dir"]),
                "client_dir": cfg["client_dir"], "client_ok": os.path.isdir(cfg["client_dir"])}
    if path == "/api/apache_status":
        r = dk("exec", "onec-server", "bash", "-lc",
               "ps -eo comm=,stat= | awk '$1 == \"apache2\" || $1 == \"httpd\" { if ($2 !~ /^Z/) found=1 } END { exit !found }' && echo running || echo stopped")
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout).strip() or "не удалось проверить Apache"}
        return {"running": r.stdout.strip().splitlines()[-1:] == ["running"]}
    if path == "/api/web_status":
        r = dk("exec", "onec-server", "bash", "-lc",
               "find /var/www/onec -mindepth 2 -maxdepth 2 -type f -name default.vrd -printf '%h\\n' 2>/dev/null")
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout).strip() or "не удалось проверить веб-публикации", "published": []}
        names = [os.path.basename(x.strip()) for x in r.stdout.splitlines() if x.strip()]
        checks = {}
        for name in names:
            # Проверяем не только наличие default.vrd, но и реальный HTTP-ответ веб-публикации.
            url = "http://127.0.0.1/" + quote(name) + "/"
            try:
                probe = subprocess.run(["curl", "-L", "-sS", "-o", "/dev/null", "-w", "%{http_code}",
                                        "--max-time", "4", url],
                                       capture_output=True, text=True, errors="replace", timeout=5)
                code = (probe.stdout or "").strip()
                checks[name] = {"available": probe.returncode == 0 and code[:1] in ("2", "3"),
                                "redirect": probe.returncode == 0 and code[:1] == "3",
                                "http_code": code or None,
                                "error": (probe.stderr or "").strip() or None}
            except Exception as e:
                checks[name] = {"available": False, "http_code": None, "error": str(e)}
        return {"published": names, "checks": checks}
    if path == "/api/containers":
        r = dk("ps", "-a", "--filter", "name=onec-", "--format", "{{json .}}")
        items = []
        for line in r.stdout.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("Names") in CONTAINERS:
                items.append({"name": d["Names"], "state": d.get("State", ""), "status": d.get("Status", ""),
                              "image": d.get("Image", ""), "ports": d.get("Ports", "")})
        return {"containers": sorted(items, key=lambda x: x["name"])}
    if path == "/api/memory":
        env = read_env(cfg["server_dir"])
        limit = (env.get("SERVER_MEMORY_LIMIT") or "0").strip()
        postgres_limit = (env.get("POSTGRES_MEMORY_LIMIT") or "0").strip()
        client_env = read_env(cfg["client_dir"])
        client_limit = (client_env.get("CLIENT_MEMORY_LIMIT") or "0").strip()
        usage, percent = "", ""
        try:
            r = dk("stats", "--no-stream", "--format", "{{.MemUsage}}|{{.MemPerc}}", "onec-server")
            if r.returncode == 0 and "|" in r.stdout:
                usage, percent = r.stdout.strip().split("|", 1)
        except Exception:
            pass
        return {"limit": limit, "postgres_limit": postgres_limit, "client_limit": client_limit,
                "usage": usage, "percent": percent}
    if path == "/api/dist":
        target = q.get("target", [""])[0]
        if target not in ("server", "postgres"):
            raise ValueError("недопустимый каталог")
        d = os.path.join(project("server"), "server" if target == "server" else "postgres", "dist")
        files = []
        if os.path.isdir(d):
            for n in os.listdir(d):
                p = os.path.join(d, n)
                if os.path.isfile(p) and not n.startswith("."):
                    files.append({"name": n, "size": os.path.getsize(p)})
        return {"dir": d, "files": sorted(files, key=lambda x: x["name"])}
    if path == "/api/logs":
        name = q.get("name", [""])[0]
        tail = q.get("tail", ["200"])[0]
        if name not in CONTAINERS or not tail.isdigit():
            raise ValueError("недопустимый запрос")
        r = dk("logs", "--tail", str(min(int(tail), 2000)), name)
        return {"text": (r.stdout + r.stderr)}
    if path in ("/api/infobases", "/api/sessions"):
        key = path.rsplit("/", 1)[1]
        r = dk("exec", "onec-server", "bash", "-c", RAC[key], "_")
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout).strip() or "rac завершился с ошибкой", "items": []}
        return {"items": parse_rac(r.stdout)}
    if path == "/api/pg":
        r = dk("exec", "onec-postgres", "runuser", "-u", "postgres", "--", "/usr/local/pgbin/psql", "-d", "postgres",
               "-At", "-F", "|", "-c",
               "SELECT datname, pg_size_pretty(pg_database_size(datname)) FROM pg_database WHERE NOT datistemplate ORDER BY 1")
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout).strip(), "items": []}
        return {"items": [dict(zip(("name", "size"), l.split("|"))) for l in r.stdout.splitlines() if "|" in l]}
    if path == "/api/files":
        # При открытии раздела «Выгрузки» создаём папки для всех зарегистрированных
        # баз, чтобы структура была видна даже до первой резервной копии.
        if cfg.get("docker"):
            try:
                result = dk("exec", "onec-server", "bash", "-lc", RAC["infobases"])
                if result.returncode == 0:
                    for item in parse_rac(result.stdout):
                        name = item.get("name") or item.get("Name")
                        if name and NAME_RE.fullmatch(name):
                            backup_base_dir(name)
            except Exception as exc:
                print("Не удалось подготовить каталоги резервных копий: %s" % exc, file=sys.stderr)
        return {"backups": list_files("backups"), "export": list_files("export"), "licenses": list_files("licenses")}
    if path == "/api/job":
        j = jobs.get(q.get("id", [""])[0])
        if not j:
            raise ValueError("задача не найдена")
        since = int(q.get("since", ["0"])[0] or 0)
        return {"title": j.title, "status": j.status, "rc": j.rc, "lines": j.lines[since:], "next": len(j.lines)}
    return None


# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    server_version = "1CManager"

    def log_message(self, *a):
        pass

    def _send(self, code, data, ctype="application/json; charset=utf-8", extra=None):
        body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _auth(self, q):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            return False
        tok = self.headers.get("X-Token") or q.get("token", [""])[0]
        return secrets.compare_digest(tok, cfg["token"])

    def do_GET(self):
        u = urlparse(self.path); q = parse_qs(u.query)
        if not self._auth(q):
            return self._send(403, {"error": "доступ запрещён: откройте ссылку с токеном из консоли"})
        try:
            if u.path == "/":
                html = open(os.path.join(HERE, "index.html"), "rb").read()
                return self._send(200, html, "text/html; charset=utf-8")
            if u.path == "/download":
                kind, name = q.get("dir", [""])[0], q.get("name", [""])[0]
                # name может быть с вложенными папками (backup/export просматриваются на веб целиком);
                # защита от выхода за пределы каталога — normpath + проверка префикса
                if kind not in ("backups", "export") or not name or name.startswith(".") or "\x00" in name:
                    return self._send(400, {"error": "недопустимый запрос"})
                base = os.path.realpath(data_dir(kind))
                p = os.path.normpath(os.path.join(base, name))
                if not (p == base or p.startswith(base + os.sep)):
                    return self._send(400, {"error": "недопустимый путь"})
                # Не разрешаем обход каталога через символьные ссылки внутри backup/export.
                p = os.path.realpath(p)
                if not (p == base or p.startswith(base + os.sep)):
                    return self._send(400, {"error": "недопустимый путь"})
                if not os.path.isfile(p):
                    return self._send(404, {"error": "файл не найден"})
                # отдаём чанками: .dt бывает в несколько ГБ, целиком в память его читать нельзя
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(os.path.getsize(p)))
                self.send_header("Content-Disposition",
                                 "attachment; filename*=UTF-8''%s" % quote(os.path.basename(p)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                with open(p, "rb") as f:
                    shutil.copyfileobj(f, self.wfile, 1024 * 1024)
                return
            res = api_get(u.path, q)
            if res is None:
                return self._send(404, {"error": "не найдено"})
            return self._send(200, res)
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        u = urlparse(self.path)
        if not self._auth({}):
            return self._send(403, {"error": "доступ запрещён"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if u.path == "/api/yandex_cf":
                if n <= 0 or n > 8192:
                    return self._send(400, {"error": "передайте ссылку на публичный файл Яндекс Диска"})
                try:
                    payload = json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    return self._send(400, {"error": "не удалось прочитать ссылку"})
                ib = str(payload.get("ib", "")).strip()
                if not NAME_RE.fullmatch(ib):
                    return self._send(400, {"error": "сначала выберите информационную базу"})
                public_url = str(payload.get("url", "")).strip()
                parsed = urlparse(public_url)
                allowed_hosts = {"disk.yandex.ru", "yadi.sk", "disk.yandex.com",
                                 "disk.yandex.kz", "disk.yandex.com.tr"}
                if (parsed.scheme != "https" or (parsed.hostname or "").lower() not in allowed_hosts
                        or not parsed.path.startswith(("/d/", "/i/")) or parsed.username or parsed.password):
                    return self._send(400, {"error": "укажите публичную HTTPS-ссылку Яндекс Диска (например, https://disk.yandex.ru/d/…)"})
                api_base = "https://cloud-api.yandex.net/v1/disk/public/resources"
                query = urlencode({"public_key": public_url})
                request = Request(api_base + "?" + query, headers={"User-Agent": "1C-Docker-Manager/1.0"})
                with urlopen(request, timeout=30) as response:
                    metadata = json.loads(response.read(1024 * 1024).decode("utf-8"))
                original_name = str(metadata.get("name", ""))
                if not original_name.lower().endswith(".cf"):
                    return self._send(400, {"error": "по ссылке найден файл «%s», нужен файл .cf" % (original_name or "без имени")})
                size = int(metadata.get("size") or 0)
                max_size = 8 * 1024 * 1024 * 1024
                if size <= 0 or size > max_size:
                    return self._send(413, {"error": "размер файла по ссылке должен быть от 1 байта до 8 ГБ"})
                request = Request(api_base + "/download?" + query,
                                  headers={"User-Agent": "1C-Docker-Manager/1.0"})
                with urlopen(request, timeout=30) as response:
                    download_info = json.loads(response.read(1024 * 1024).decode("utf-8"))
                href = str(download_info.get("href", ""))
                href_info = urlparse(href)
                if href_info.scheme != "https" or not href_info.hostname:
                    return self._send(502, {"error": "Яндекс Диск вернул некорректную ссылку скачивания"})
                safe_stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", os.path.splitext(os.path.basename(original_name))[0]).strip("._-")
                safe_name = ((safe_stem[:150] or "yandex_config") + ".cf")
                dest_dir = backup_base_dir(ib, "cf")
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, safe_name)
                part = os.path.join(dest_dir, ".yandex-" + secrets.token_hex(8) + ".part")
                downloaded = 0
                try:
                    with urlopen(Request(href, headers={"User-Agent": "1C-Docker-Manager/1.0"}), timeout=60) as response:
                        with open(part, "xb") as output:
                            while True:
                                chunk = response.read(1024 * 1024)
                                if not chunk:
                                    break
                                downloaded += len(chunk)
                                if downloaded > max_size or downloaded > size:
                                    raise ValueError("размер скачанного файла превышает размер, указанный Яндекс Диском")
                                output.write(chunk)
                            output.flush()
                            os.fsync(output.fileno())
                    if downloaded != size:
                        raise ValueError("файл скачан не полностью: получено %s из %s байт" % (downloaded, size))
                    os.replace(part, dest)
                    os.chmod(dest, 0o666)
                except Exception:
                    try: os.unlink(part)
                    except OSError: pass
                    raise
                return self._send(200, {"filename": safe_name, "size": downloaded,
                                        "kind": "cf", "path": dest, "source": "yandex"})
            if u.path == "/api/upload":
                if n <= 0:
                    return self._send(400, {"error": "загружаемый файл пуст"})
                filename = parse_qs(u.query).get("name", [""])[0]
                filename = os.path.basename(filename).strip()
                if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,176}\.(dt|cf)", filename, re.I)
                        or filename.startswith(".")):
                    return self._send(400, {"error": "разрешены только файлы .dt и .cf (имя до 180 символов)"})
                ext = filename.rsplit(".", 1)[1].lower()
                ib = parse_qs(u.query).get("ib", [""])[0]
                if not NAME_RE.fullmatch(ib):
                    return self._send(400, {"error": "не указана корректная информационная база"})
                # .dt и .cf хранятся отдельно для каждой информационной базы.
                # затем атомарно заменяем целевой: оборванная загрузка не уничтожит старую копию.
                safe_name = filename
                dest_dir = backup_base_dir(ib, ext)
                dest = os.path.join(dest_dir, safe_name)
                part = os.path.join(dest_dir, ".upload-" + secrets.token_hex(8) + ".part")
                remaining = n
                try:
                    with open(part, "xb") as f:
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("соединение прервано во время загрузки")
                            f.write(chunk); remaining -= len(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(part, dest)
                    os.chmod(dest, 0o666)
                except Exception:
                    try: os.unlink(part)
                    except OSError: pass
                    raise
                return self._send(200, {"filename": safe_name, "size": n, "kind": ext, "path": dest, "backup_dir": dest_dir, "ib": ib})
            if u.path == "/api/upload_license":
                if n <= 0 or n > 50 * 1024 * 1024:
                    return self._send(413, {"error": "размер файла лицензии должен быть от 1 байта до 50 МБ"})
                filename = os.path.basename(parse_qs(u.query).get("name", [""])[0]).strip()
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,176}\.lic", filename, re.I) or filename.startswith("."):
                    return self._send(400, {"error": "выберите файл лицензии с расширением .lic"})
                dest_dir = data_dir("licenses")
                os.makedirs(dest_dir, exist_ok=True)
                dest = os.path.join(dest_dir, filename)
                part = os.path.join(dest_dir, ".license-upload-" + secrets.token_hex(8) + ".part")
                remaining = n
                try:
                    with open(part, "xb") as f:
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("соединение прервано во время загрузки лицензии")
                            f.write(chunk); remaining -= len(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(part, dest)
                    os.chmod(dest, 0o666)
                except Exception:
                    try: os.unlink(part)
                    except OSError: pass
                    raise
                return self._send(200, {"filename": filename, "size": n, "path": dest})
            if u.path == "/api/upload_pg_dump":
                if n <= 0 or n > 8 * 1024 * 1024 * 1024:
                    return self._send(413, {"error": "размер файла должен быть от 1 байта до 8 ГБ"})
                filename = os.path.basename(parse_qs(u.query).get("name", [""])[0]).strip()
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,176}\.dump", filename, re.I) or filename.startswith("."):
                    return self._send(400, {"error": "разрешены только файлы .dump"})
                db = parse_qs(u.query).get("db", [""])[0]
                if not NAME_RE.fullmatch(db):
                    return self._send(400, {"error": "сначала выберите целевую базу PostgreSQL"})
                dest_dir = backup_base_dir(db, "pg")
                dest = os.path.join(dest_dir, filename)
                part = os.path.join(dest_dir, ".upload-" + secrets.token_hex(8) + ".part")
                remaining = n
                try:
                    with open(part, "xb") as f:
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("соединение прервано во время загрузки")
                            f.write(chunk); remaining -= len(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(part, dest)
                    os.chmod(dest, 0o666)
                except Exception:
                    try: os.unlink(part)
                    except OSError: pass
                    raise
                return self._send(200, {"filename": filename, "size": n, "path": dest})
            if u.path == "/api/upload_dist":
                target = parse_qs(u.query).get("target", [""])[0]
                if target not in ("server", "postgres"):
                    return self._send(400, {"error": "недопустимый каталог"})
                if n <= 0 or n > 4 * 1024 * 1024 * 1024:
                    return self._send(413, {"error": "размер файла должен быть от 1 байта до 4 ГБ"})
                filename = os.path.basename(parse_qs(u.query).get("name", [""])[0]).strip()
                if (not filename or filename.startswith(".") or len(filename) > 180 or
                        any(ord(c) < 32 for c in filename) or
                        os.path.splitext(filename)[1].lower() not in (".run", ".deb")):
                    return self._send(400, {"error": "разрешены только файлы .run и .deb"})
                d = os.path.join(project("server"), "server" if target == "server" else "postgres", "dist")
                os.makedirs(d, exist_ok=True)
                dest = os.path.join(d, filename)
                part = os.path.join(d, ".dist-upload-" + secrets.token_hex(8) + ".part")
                remaining = n
                try:
                    with open(part, "wb") as f:
                        while remaining:
                            chunk = self.rfile.read(min(1024 * 1024, remaining))
                            if not chunk:
                                raise ValueError("соединение прервано во время загрузки")
                            f.write(chunk); remaining -= len(chunk)
                    os.replace(part, dest)
                    os.chmod(dest, 0o644)
                except Exception:
                    try: os.unlink(part)
                    except OSError: pass
                    raise
                return self._send(200, {"filename": filename, "size": n, "dir": d})
            if n > 100000:
                return self._send(413, {"error": "слишком большой запрос"})
            body = json.loads(self.rfile.read(n) or b"{}")
            if u.path == "/api/settings":
                return self._send(200, save_settings(body))
            if u.path == "/api/backup_schedule":
                return self._send(200, save_backup_schedule_request(body))
            if u.path != "/api/action":
                return self._send(404, {"error": "не найдено"})
            if not cfg["docker"] and body.get("action") in ("container_op", "compose_op", "infobase_create", "infobase_drop", "session_kill", "import_file", "start_server_with_dt", "restore_pg_dump", "set_memory", "recreate_server", "license_server_activate", "apache_op", "web_publish", "web_delete"):
                return self._send(400, {"error": "Docker недоступен для этого пользователя"})
            return self._send(200, {"job": do_action(body)})
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)})


def main():
    ap = argparse.ArgumentParser(description="Менеджер контейнеров 1С")
    ap.add_argument("--server-dir", default=os.path.join(HERE, "..", "1c-server"))
    ap.add_argument("--client-dir", default=os.path.join(HERE, "..", "1c-client"))
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token", default=None, help="фиксированный токен (по умолчанию случайный)")
    ap.add_argument("--no-browser", action="store_true")
    a = ap.parse_args()
    cfg.update(server_dir=os.path.abspath(a.server_dir), client_dir=os.path.abspath(a.client_dir),
               token=a.token or secrets.token_urlsafe(16), docker=detect_docker())
    load_backup_schedule()
    migrate_legacy_backups()
    threading.Thread(target=automatic_backup_loop, daemon=True, name="automatic-backups").start()
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    except OSError as e:
        print("Не удалось запустить веб-панель на порту %d: %s" % (a.port, e), file=sys.stderr)
        print("Возможно, другая копия панели уже работает. Остановите её или выберите другой порт через --port.", file=sys.stderr)
        return 2
    url = "http://127.0.0.1:%d/?token=%s" % (a.port, cfg["token"])
    print("Менеджер 1С запущен. Откройте в браузере:\n  %s" % url)
    print("Docker: %s" % (" ".join(cfg["docker"]) if cfg["docker"] else "НЕДОСТУПЕН (см. подсказку в панели)"))
    print("Остановка: Ctrl+C")
    if not a.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено")


if __name__ == "__main__":
    raise SystemExit(main())
