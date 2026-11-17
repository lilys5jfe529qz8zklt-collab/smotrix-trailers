from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / ".runtime" / "trailers.zip"
CREDENTIALS = Path("C:/Users/KHACKIRAEV/Desktop/happ-clone/hapi-app/.runtime/bothost.txt")
ENV_FILE = ROOT / ".secrets" / "trailer.env"
TOKEN_FILE = ROOT / ".secrets" / "trailer-bot-token.txt"

NAME = "720Trailers"
MAIN_FILE = "main.py"
PY_VERSION = "3.11"
INTERNAL_PORT = "3000"

INCLUDE = [
    "main.py", "config.py", "db.py", "matcher.py", "youtube.py",
    "telegram_client.py", "media.py", "worker.py", "web.py",
    "requirements.txt",
    "miniapp/index.html", "miniapp/app.js", "miniapp/app.css",
]

TOKEN_RE = re.compile(r"\b\d{6,15}:[A-Za-z0-9_-]{30,}\b")
ID_RE = re.compile(r"bot_\d+_\d+_[a-z0-9]+")


def redact(value):
    if isinstance(value, dict):
        return {k: ("***" if re.search(r"token|secret|password|env|cookie", k, re.I) else redact(v))
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return TOKEN_RE.sub("[TOKEN]", value)
    return value


def env_values() -> dict[str, str]:
    values = {}
    for line in ENV_FILE.read_text("utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    return values


def credentials() -> dict[str, str]:
    values = {}
    for line in CREDENTIALS.read_text("utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep:
            values[k.strip().lower()] = v.strip()
    return values


def build_archive() -> None:
    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ARCHIVE, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name in INCLUDE:
            path = ROOT / name
            if not path.is_file():
                raise SystemExit(f"missing {name}")
            zf.write(path, name)
    print("archive", ARCHIVE.stat().st_size, "bytes")


def login() -> requests.Session:
    values = credentials()
    session = requests.Session()
    r = session.post(
        "https://bothost.ru/login",
        data={"email": values["login"], "password": values["password"]},
        timeout=45,
    )
    r.raise_for_status()
    if "loginForm" in r.text:
        raise SystemExit("login failed")
    return session


def find_bot_id(html: str) -> str | None:
    ids = ID_RE.findall(html)
    return ids[-1] if ids else None


def create_bot(session: requests.Session, token: str) -> str:
    r = session.post(
        "https://bothost.ru/create-bot.php",
        data={
            "name": NAME,
            "platform": "telegram",
            "location": "eu",
            "bot_token": token,
            "has_web_interface": "1",
            "internal_port": INTERNAL_PORT,
            "use_custom_dockerfile": "0",
            "main_file": MAIN_FILE,
            "python_version": PY_VERSION,
        },
        timeout=120,
        allow_redirects=True,
    )
    (ROOT / ".runtime" / "create-result.html").write_text(r.text, encoding="utf-8")
    bot_id = find_bot_id(r.text)
    print("create HTTP", r.status_code, "ids", set(ID_RE.findall(r.text)))
    if not bot_id:
        raise SystemExit("bot id not found in create response")
    return bot_id


def set_env(session: requests.Session, bot_id: str, data: dict[str, str]) -> dict:
    r = session.post(
        "https://bothost.ru/api/bot-settings.php",
        json={"action": "update_env", "id": bot_id, "environment_variables": data},
        timeout=60,
    )
    try:
        return r.json()
    except ValueError:
        return {"status": r.status_code, "text": r.text[:300]}


def set_domain(session: requests.Session, bot_id: str, domain: str) -> dict:
    r = session.post(
        "https://bothost.ru/api/bot-settings.php",
        json={
            "action": "update_domain", "id": bot_id, "enabled": True,
            "domain": domain, "internal_port": int(INTERNAL_PORT),
        },
        timeout=60,
    )
    try:
        return r.json()
    except ValueError:
        return {"status": r.status_code, "text": r.text[:300]}


def deploy(session: requests.Session, bot_id: str) -> dict:
    with ARCHIVE.open("rb") as handle:
        r = session.post(
            "https://bothost.ru/api/deploy-archive.php",
            data={"bot_id": bot_id},
            files={"archive": (ARCHIVE.name, handle, "application/zip")},
            timeout=(30, 900),
        )
    try:
        return r.json()
    except ValueError:
        return {"status": r.status_code, "text": r.text[:400]}


def domain_for(bot_id: str) -> str:
    return bot_id.replace("_", "-") + ".bothost.tech"


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    action = sys.argv[1] if len(sys.argv) > 1 else "all"
    build_archive()
    token = TOKEN_FILE.read_text("utf-8").strip()
    session = login()
    state_file = ROOT / ".runtime" / "deploy-state.json"
    state = json.loads(state_file.read_text("utf-8")) if state_file.exists() else {}
    bot_id = state.get("bot_id")
    if action in {"create", "all"} and not bot_id:
        bot_id = create_bot(session, token)
        state["bot_id"] = bot_id
        state_file.write_text(json.dumps(state), encoding="utf-8")
    if not bot_id:
        raise SystemExit("no bot id; run create")
    domain = domain_for(bot_id)
    values = env_values()
    env = {
        "BOT_TOKEN": token,
        "TELEGRAM_API_ID": values.get("TELEGRAM_API_ID", ""),
        "TELEGRAM_API_HASH": values.get("TELEGRAM_API_HASH", ""),
        "TRAILER_CHAT_ID": values.get("TRAILER_CHAT_ID", ""),
        "ADMIN_IDS": values.get("ADMIN_IDS", "5820040561"),
        "STORAGE_API_BASE": values.get("STORAGE_API_BASE", ""),
        "STORAGE_TOKEN": values.get("STORAGE_TOKEN", ""),
        "MEDIA_SECRET": values.get("MEDIA_SECRET", ""),
        "TRAILER_TOKEN": values.get("TRAILER_TOKEN", ""),
        "DATA_DIR": "/app/data",
        "PORT": INTERNAL_PORT,
        "PUBLIC_URL": "https://" + domain,
        "YOUTUBE_CHANNELS": values.get("YOUTUBE_CHANNELS", "@kinomantrailers"),
        "DEFAULT_QUALITY": values.get("DEFAULT_QUALITY", "1080"),
        "MAX_FILE_MB": values.get("MAX_FILE_MB", "1900"),
        "POLL_MINUTES": values.get("POLL_MINUTES", "15"),
    }
    if action in {"env", "all"}:
        print("env:", json.dumps(redact(set_env(session, bot_id, env)), ensure_ascii=False)[:400])
    if action in {"domain", "all"}:
        print("domain:", json.dumps(redact(set_domain(session, bot_id, domain)), ensure_ascii=False)[:400])
    if action in {"deploy", "all"}:
        print("deploy:", json.dumps(redact(deploy(session, bot_id)), ensure_ascii=False)[:600])
    state["domain"] = domain
    state_file.write_text(json.dumps(state), encoding="utf-8")
    print("BOT_ID", bot_id)
    print("DOMAIN", domain)


if __name__ == "__main__":
    main()
