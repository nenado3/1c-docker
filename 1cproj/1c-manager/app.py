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


def data_dir(kind):
    if kind == "backups":
        # Для DT/CF используем строго каталог проекта 1c-server/backups,
        # не допускаем расхождения с ожидаемыми backups/dt и backups/cf.
        return os.path.join(cfg["server_dir"], "backups")
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
            # память сервера задаётся сразу при создании образов: пишем в .env, применится при запуске
            mem = str(body.get("memory", "")).strip().lower()
            if mem:
                if not MEM_RE.match(mem):
                    raise ValueError("лимит памяти: 0 или число с суффиксом, например 8g, 512m")
                write_env_value(os.path.join(project("server"), ".env"), "SERVER_MEMORY_LIMIT", mem)
        return script_job("Сборка образов (%s)" % proj, proj, "build.sh", *args)
    if a == "update_image":
        proj = need(body, "project")
        if proj not in ("server", "client"):
            raise ValueError("недопустимый проект")
        return script_job("Обновление Docker-образа (%s)" % proj, proj, "update-image.sh")
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
        user, pwd = str(body.get("user", "")).strip(), str(body.get("pwd", ""))
        # реквизиты PostgreSQL можно задать в панели; пустые — берутся из .env
        db_user = str(body.get("db_user", "")).strip()
        db_pwd = str(body.get("db_pwd", ""))
        if db_user and not NAME_RE.match(db_user):
            raise ValueError("недопустимое имя пользователя PostgreSQL")
        if len(db_pwd) > 200:
            raise ValueError("слишком длинный пароль PostgreSQL")
        # Передаём import.sh тот же абсолютный путь, куда API загрузки реально сохранил файл.
        # Это исключает расхождение BACKUP_DIR/cwd между веб-панелью и shell-скриптом.
        uploaded_file = os.path.join(data_dir("backups"), kind, filename)
        if not os.path.isfile(uploaded_file):
            raise ValueError("Загрузка не найдена на диске: %s. Проверьте ответ загрузки и каталог BACKUP_DIR." % uploaded_file)
        if os.path.getsize(uploaded_file) <= 0:
            raise ValueError("Загруженный файл пустой: %s" % uploaded_file)
        args = [ib, filename, kind, user, pwd, db_user, db_pwd, uploaded_file]
        return script_job("Импорт .%s в базу %s" % (kind, ib), "server", "import.sh", *args)
    if a == "backup_pg":
        return script_job("Дамп PostgreSQL %s" % need(body, "db", NAME_RE), "server", "backup.sh", "--pg", need(body, "db", NAME_RE))
    if a == "client_start":
        return script_job("Запуск клиента 1С", "client", "client.sh")
    if a == "license_install":
        return script_job("Перенос лицензий", "client", "license.sh", "install")
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


def api_get(path, q):
    if path == "/api/status":
        return {"docker": bool(cfg["docker"]), "docker_cmd": " ".join(cfg["docker"] or []),
                "server_dir": cfg["server_dir"], "server_ok": os.path.isdir(cfg["server_dir"]),
                "client_dir": cfg["client_dir"], "client_ok": os.path.isdir(cfg["client_dir"])}
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
        usage, percent = "", ""
        try:
            r = dk("stats", "--no-stream", "--format", "{{.MemUsage}}|{{.MemPerc}}", "onec-server")
            if r.returncode == 0 and "|" in r.stdout:
                usage, percent = r.stdout.strip().split("|", 1)
        except Exception:
            pass
        return {"limit": limit, "usage": usage, "percent": percent}
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
                if n <= 0 or n > 8 * 1024 * 1024 * 1024:
                    return self._send(413, {"error": "размер файла должен быть от 1 байта до 8 ГБ"})
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
            if u.path != "/api/action":
                return self._send(404, {"error": "не найдено"})
            if not cfg["docker"] and body.get("action") in ("container_op", "compose_op", "infobase_create", "infobase_drop", "session_kill", "import_file", "set_memory"):
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
