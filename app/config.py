import json
import os
from pathlib import Path
from typing import Any
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

TURSO_DATABASE_URL = os.getenv("TURSO_DATABASE_URL", "").strip()
TURSO_AUTH_TOKEN = os.getenv("TURSO_AUTH_TOKEN", "").strip()
APP_PASSWORD = os.getenv("APP_PASSWORD", "").strip()

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA_DIR / "phsellstats.db"
SESSIONS_DIR = DATA_DIR / "sessions"
SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

CAPTURES_DIR = DATA_DIR / "captures"
CAPTURES_DIR.mkdir(parents=True, exist_ok=True)

CONFIG_PATH = DATA_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "active_sync_interval_minutes": int(os.getenv("ACTIVE_SYNC_INTERVAL_MINUTES", "10")),
    "idle_sync_interval_minutes": int(os.getenv("IDLE_SYNC_INTERVAL_MINUTES", "120")),
    "currency": "ARS",
    "lumepic_enabled": True,
    "momentia_enabled": True,
    "lumepic_login_url": os.getenv("LUMEPIC_LOGIN_URL", "https://photographer.lumepic.com/es/sales"),
    "lumepic_dashboard_url": os.getenv("LUMEPIC_DASHBOARD_URL", "https://photographer.lumepic.com/es/sales"),
    "momentia_login_url": os.getenv("MOMENTIA_LOGIN_URL", "https://moment-ia.shop/photographer/dashboard?section=sales"),
    "momentia_dashboard_url": os.getenv("MOMENTIA_DASHBOARD_URL", "https://moment-ia.shop/photographer/dashboard?section=sales"),
    "seed_demo_on_empty": os.getenv("SEED_DEMO_ON_EMPTY", "true").lower() == "true",
}


def load_config() -> dict[str, Any]:
    config = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                config.update(saved)
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_config(updates: dict[str, Any]) -> dict[str, Any]:
    config = load_config()
    for key, value in updates.items():
        if key in DEFAULT_CONFIG and value is not None:
            config[key] = value
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
    return config
