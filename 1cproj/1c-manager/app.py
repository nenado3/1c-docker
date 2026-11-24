#!/usr/bin/env python3
"""Менеджер контейнеров 1С: локальная веб-панель (только стандартная библиотека Python).
Слушает только 127.0.0.1, требует случайный токен, выполняет лишь заранее заданные действия."""
import argparse, json, os, re, secrets, shutil, subprocess, sys, threading, time, webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = {}
jobs, jobs_lock = {}, threading.Lock()
CONTAINERS = {"onec-server", "onec-postgres", "onec-client"}
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
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
        uploaded_file = os.path.join(data_dir("backups"), "dt", filename)
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
        uploaded_file = os.path.join(data_dir("backups"), kind, filename)
        if not os.path.isfile(uploaded_file):
            raise ValueError("Загрузка не найдена на диске: %s. Проверьте ответ загрузки и каталог BACKUP_DIR." % uploaded_file)
        if os.path.getsize(uploaded_file) <= 0:
            raise ValueError("Загруженный файл пустой: %s" % uploaded_file)
        if new_base and kind != "dt":
            raise ValueError("создание новой базы поддерживается только из файла .dt")
        # Девятый аргумент включает строгий режим: только новая ИБ и новая БД PostgreSQL.
        args = [ib, filename, kind, user, pwd, db_user, db_pwd, uploaded_file, "new" if new_base else "", db_name if new_base else ""]
        title = "Создание базы %s из .dt" % ib if new_base else "Импорт .%s в базу %s" % (kind, ib)
        return script_job(title, "server", "import.sh", *args)
    if a == "backup_pg":
        return script_job("Дамп PostgreSQL %s" % need(body, "db", NAME_RE), "server", "backup.sh", "--pg", need(body, "db", NAME_RE))
    if a == "restore_pg_dump":
        db = need(body, "db", NAME_RE)
        filename = need(body, "filename", re.compile(r"^[A-Za-z0-9_.-]{1,180}\.dump$", re.I), 190)
        uploaded_file = os.path.join(data_dir("backups"), "pg", filename)
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


# ---------------- автоматические резервные копии ----------------
backup_schedule_lock = threading.Lock()
backup_schedule = {"enabled": False, "interval_hours": 24, "last_run": None, "next_run": None, "last_result": "Расписание не настроено"}
backup_schedule_path = None
backup_running = False


def save_backup_schedule():
    if not backup_schedule_path:
        return
    tmp = backup_schedule_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(backup_schedule, f, ensure_ascii=False, indent=2)
    os.replace(tmp, backup_schedule_path)


def load_backup_schedule():
    global backup_schedule_path
    backup_schedule_path = os.path.join(cfg["server_dir"], ".backup-schedule.json")
    try:
        with open(backup_schedule_path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            hours = int(data.get("interval_hours", 24))
            if 1 <= hours <= 720:
                backup_schedule.update(data)
                backup_schedule["interval_hours"] = hours
    except FileNotFoundError:
        pass
    except Exception as e:
        print("Не удалось прочитать расписание бэкапов: %s" % e, file=sys.stderr)


def backup_schedule_snapshot():
    with backup_schedule_lock:
        result = dict(backup_schedule)
        result["running"] = backup_running
        return result


def save_backup_schedule_request(body):
    enabled = body.get("enabled") is True
    try:
        hours = int(body.get("interval_hours", 24))
    except (TypeError, ValueError):
        raise ValueError("Интервал должен быть числом часов")
    if hours < 1 or hours > 720:
        raise ValueError("Интервал должен быть от 1 до 720 часов")
    selected = body.get("selected_bases", [])
    if not isinstance(selected, list) or any(not isinstance(x, str) or not NAME_RE.fullmatch(x) for x in selected):
        raise ValueError("Некорректный список информационных баз")
    if len(selected) != len(set(selected)):
        raise ValueError("В списке баз есть повторения")
    if enabled and not selected:
        raise ValueError("Выберите хотя бы одну базу для автоматического резервного копирования")
    with backup_schedule_lock:
        backup_schedule["selected_bases"] = selected
        backup_schedule["enabled"] = enabled
        backup_schedule["interval_hours"] = hours
        backup_schedule["next_run"] = time.time() + hours * 3600 if enabled else None
        backup_schedule["last_result"] = ("Расписание включено; первая копия будет создана через %d ч." % hours
                                          if enabled else "Автоматическое резервное копирование выключено")
        save_backup_schedule()
        result = dict(backup_schedule)
        result["running"] = backup_running
        return result


def automatic_backup_loop():
    global backup_running
    while True:
        time.sleep(15)
        with backup_schedule_lock:
            due = backup_schedule.get("enabled") and backup_schedule.get("next_run") and time.time() >= backup_schedule["next_run"]
            if not due or backup_running:
                continue
            backup_running = True
            hours = int(backup_schedule["interval_hours"])
            backup_schedule["next_run"] = time.time() + hours * 3600
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
                proc = subprocess.run(["bash", os.path.join(cfg["server_dir"], "backup.sh"), name],
                                      cwd=cfg["server_dir"], env=backup_env, capture_output=True, text=True,
                                      errors="replace", timeout=6 * 3600)
                result_lines.append("[%s] %s" % (name, "OK" if proc.returncode == 0 else "ОШИБКА"))
                if proc.stdout:
                    result_lines.append(proc.stdout[-5000:])
                if proc.stderr:
                    result_lines.append(proc.stderr[-2000:])
                if proc.returncode:
                    raise RuntimeError("Ошибка резервного копирования базы %s" % name)
            summary = "Успешно: " + ", ".join(bases)
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
               "if pgrep -x apache2 >/dev/null 2>&1; then echo running; else echo stopped; fi")
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout).strip() or "не удалось проверить Apache"}
        return {"running": r.stdout.strip().splitlines()[-1:] == ["running"]}
    if path == "/api/web_status":
        r = dk("exec", "onec-server", "bash", "-lc",
               "find /var/www/onec -mindepth 2 -maxdepth 2 -type f -name default.vrd -printf '%h\\n' 2>/dev/null")
        if r.returncode != 0:
            return {"error": (r.stderr or r.stdout).strip() or "не удалось проверить веб-публикации", "published": []}
        names = [os.path.basename(x.strip()) for x in r.stdout.splitlines() if x.strip()]
        return {"published": names}
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
            if u.path == "/api/upload":
                if n <= 0:
                    return self._send(400, {"error": "загружаемый файл пуст"})
                filename = parse_qs(u.query).get("name", [""])[0]
                filename = os.path.basename(filename).strip()
                if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,176}\.(dt|cf)", filename, re.I)
                        or filename.startswith(".")):
                    return self._send(400, {"error": "разрешены только файлы .dt и .cf (имя до 180 символов)"})
                ext = filename.rsplit(".", 1)[1].lower()
                # .dt и .cf хранятся раздельно. Сначала пишем во временный файл,
                # затем атомарно заменяем целевой: оборванная загрузка не уничтожит старую копию.
                safe_name = filename
                dest_dir = os.path.join(data_dir("backups"), ext)
                os.makedirs(dest_dir, exist_ok=True)
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
                return self._send(200, {"filename": safe_name, "size": n, "kind": ext, "path": dest, "backup_dir": data_dir("backups")})
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
                dest_dir = os.path.join(data_dir("backups"), "pg")
                os.makedirs(dest_dir, exist_ok=True)
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
