from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates


APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR.parent / "static"


def static_url(path: str) -> str:
    normalized = path.lstrip("/")
    if normalized.startswith("static/"):
        relative_path = normalized.removeprefix("static/")
        url_path = f"/{normalized}"
    else:
        relative_path = normalized
        url_path = f"/static/{normalized}"

    static_path = STATIC_DIR / relative_path
    try:
        version = static_path.stat().st_mtime_ns
    except OSError:
        return url_path
    return f"{url_path}?v={version}"


templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.globals["static_url"] = static_url
