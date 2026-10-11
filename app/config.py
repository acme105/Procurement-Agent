"""Settings. Everything can be overridden with environment variables (see .env.example).

Secrets (OPENAI_API_KEY, SMTP_*) are read from the environment or from ~/.config/aerchain/*.env, never from the repo.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = Path(os.environ.get("AERCHAIN_STATE", ROOT / "state"))
WEB_DIR = ROOT / "web"
CATALOG = Path(os.environ.get("AERCHAIN_CATALOG", ROOT / "data" / "catalog.csv"))
IST = timezone(timedelta(hours=5, minutes=30))

# ---- Model -------------------------------------------------------------------------------
MODEL = os.environ.get("AERCHAIN_MODEL", "gpt-6-luna")
# job -> (reasoning effort, max output tokens)
JOBS: dict[str, tuple[str, int]] = {
    "live_extract": ("medium", 32000),
    "live_param": ("low", 2000),
}
MODELS: dict[str, str] = {job: os.environ.get(f"AERCHAIN_MODEL_{job.upper()}", MODEL) for job in JOBS}
LLM_CONCURRENCY = 4
LLM_TIMEOUT_S = 180
LLM_RETRIES = 2

# ---- Output ------------------------------------------------------------------------------
CHROME = os.environ.get("AERCHAIN_CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CHROME_NO_SANDBOX = os.environ.get("AERCHAIN_CHROME_NO_SANDBOX") == "1"


def _env_file_value(filename: str, key: str) -> str | None:
    f = Path.home() / ".config" / "aerchain" / filename
    if f.exists():
        for line in f.read_text().splitlines():
            line = line.strip().removeprefix("export ").strip()
            if line.startswith(f"{key}="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return None


def openai_api_key() -> str | None:
    """OPENAI_API_KEY from the environment, else ~/.config/aerchain/openai.env. Never logged."""
    return os.environ.get("OPENAI_API_KEY") or _env_file_value("openai.env", "OPENAI_API_KEY")


def real_now() -> datetime:
    """Wall-clock time in IST."""
    return datetime.now(IST)
