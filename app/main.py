import asyncio
import hashlib
import hmac
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import APP_PIN, BASE_DIR, SESSION_SECRET, load_config, save_config
from app.database import (
    clear_demo_data,
    evaluate_goals,
    get_available_months,
    get_conn,
    get_events_comparison,
    get_goals_with_progress,
    get_recent_sales,
    get_summary,
    get_timeseries,
    has_any_sales,
    init_db,
)
from app.scheduler import (
    get_scheduler_status,
    get_scraper,
    reschedule_sync,
    start_scheduler,
    stop_scheduler,
    sync_all_platforms,
)
from app.scrapers.demo_seed import seed_demo_data

STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    cfg = load_config()
    if cfg.get("seed_demo_on_empty", True) and not has_any_sales():
        seed_demo_data()
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="PhSellStats", version="1.0.0", lifespan=lifespan)

# Brute-force protection state: ip -> {"fails": int, "locked_until": float}
_login_attempts: dict[str, dict[str, float]] = {}
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_SECONDS = 300
SESSION_COOKIE_NAME = "phsellstats_session"


def _sign_session(ts: str) -> str:
    sig = hmac.new(SESSION_SECRET.encode("utf-8"), ts.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{ts}.{sig}"


def _verify_session(cookie_val: Optional[str]) -> bool:
    if not cookie_val or "." not in cookie_val:
        return False
    ts, sig = cookie_val.split(".", 1)
    expected = hmac.new(SESSION_SECRET.encode("utf-8"), ts.encode("utf-8"), hashlib.sha256).hexdigest()
    if not secrets.compare_digest(sig, expected):
        return False
    try:
        age = time.time() - int(ts)
        return 0 <= age <= 60 * 60 * 24 * 30  # 30 days
    except ValueError:
        return False


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if fwd:
        return fwd
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def session_auth_middleware(request: Request, call_next):
    path = request.url.path
    if APP_PIN and not (
        path == "/healthz"
        or path == "/login"
        or path.startswith("/api/auth/")
        or path.startswith("/static/")
    ):
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if not _verify_session(token):
            if path.startswith("/api/"):
                return JSONResponse(status_code=401, content={"detail": "No autenticado"})
            return RedirectResponse(url="/login", status_code=302)
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/login")
async def login_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "login.html")


class PinLogin(BaseModel):
    pin: str


@app.post("/api/auth/login")
async def api_auth_login(payload: PinLogin, request: Request):
    ip = _client_ip(request)
    now = time.time()
    rec = _login_attempts.get(ip, {"fails": 0, "locked_until": 0.0})

    if rec["locked_until"] > now:
        remaining = int(rec["locked_until"] - now) + 1
        raise HTTPException(
            status_code=429,
            detail=f"Demasiados intentos fallidos. Esperá {remaining} segundos.",
        )

    if not APP_PIN or secrets.compare_digest(payload.pin.strip(), APP_PIN):
        _login_attempts.pop(ip, None)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(
            key=SESSION_COOKIE_NAME,
            value=_sign_session(str(int(now))),
            httponly=True,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,
        )
        return resp

    await asyncio.sleep(0.4)
    fails = int(rec["fails"]) + 1
    if fails >= MAX_LOGIN_ATTEMPTS:
        _login_attempts[ip] = {"fails": 0, "locked_until": now + LOCKOUT_SECONDS}
        raise HTTPException(
            status_code=429,
            detail=f"Demasiados intentos fallidos. Bloqueado por {LOCKOUT_SECONDS // 60} minutos.",
        )

    _login_attempts[ip] = {"fails": fails, "locked_until": 0.0}
    left = MAX_LOGIN_ATTEMPTS - fails
    raise HTTPException(
        status_code=401,
        detail=f"PIN incorrecto. Quedan {left} intento(s) antes del bloqueo.",
    )


@app.post("/api/auth/logout")
async def api_auth_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE_NAME)
    return resp


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


class GoalCreate(BaseModel):
    name: str
    platform: str = "all"
    event_name: Optional[str] = None
    metric: str = "revenue"
    target_value: float


class ManualSaleCreate(BaseModel):
    event_name: str
    amount: float
    sold_at: Optional[str] = None
    notes: Optional[str] = None


class ConfigUpdate(BaseModel):
    active_sync_interval_minutes: Optional[int] = None
    idle_sync_interval_minutes: Optional[int] = None
    currency: Optional[str] = None
    lumepic_enabled: Optional[bool] = None
    momentia_enabled: Optional[bool] = None
    lumepic_login_url: Optional[str] = None
    lumepic_dashboard_url: Optional[str] = None
    momentia_login_url: Optional[str] = None
    momentia_dashboard_url: Optional[str] = None


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/months")
async def api_months():
    return {"months": get_available_months()}


@app.get("/api/summary")
async def api_summary(
    platform: str = Query("all"),
    event_name: str = Query("all"),
    days: int = Query(30),
    date_mode: str = Query("sale_date", pattern="^(sale_date|event_date)$"),
    month: str = Query("all"),
):
    return get_summary(platform=platform, event_name=event_name, days=days, date_mode=date_mode, month=month)


@app.get("/api/timeseries")
async def api_timeseries(
    granularity: str = Query("hour", pattern="^(hour|day|month)$"),
    platform: str = Query("all"),
    event_name: str = Query("all"),
    days: int = Query(30),
    date_mode: str = Query("sale_date", pattern="^(sale_date|event_date)$"),
    month: str = Query("all"),
):
    return get_timeseries(
        granularity=granularity,
        platform=platform,
        event_name=event_name,
        days=days,
        date_mode=date_mode,
        month=month,
    )


@app.get("/api/events")
async def api_events(
    days: int = Query(90),
    platform: str = Query("all"),
    date_mode: str = Query("sale_date", pattern="^(sale_date|event_date)$"),
    month: str = Query("all"),
):
    comparison = get_events_comparison(days=days, platform=platform, date_mode=date_mode, month=month)
    names = sorted({item["event_name"] for item in comparison})
    return {"events": comparison, "event_names": names}


@app.get("/api/sales")
async def api_sales(
    platform: str = Query("all"),
    event_name: str = Query("all"),
    limit: int = Query(50, ge=1, le=500),
):
    return get_recent_sales(platform=platform, event_name=event_name, limit=limit)


@app.post("/api/sales/manual")
async def api_create_manual_sale(payload: ManualSaleCreate):
    from datetime import datetime
    from app.database import upsert_sales_batch

    sold_at = payload.sold_at or datetime.now().isoformat(timespec="seconds")
    ext_id = f"manual_{payload.event_name.strip().lower().replace(' ', '_')}_{sold_at[:10]}"
    res = upsert_sales_batch(
        "canon",
        [
            {
                "external_id": ext_id,
                "event_external_id": ext_id,
                "event_name": payload.event_name.strip(),
                "event_date": sold_at[:10],
                "sold_at": sold_at,
                "amount": float(payload.amount),
                "items_count": 1,
                "buyer_name": payload.notes or "Canon fijo",
                "status": "completed",
                "raw": {"manual": True, "notes": payload.notes},
            }
        ],
        is_demo=0,
    )
    evaluate_goals()
    return {"ok": True, "result": res}


@app.get("/api/goals")
async def api_get_goals():
    return get_goals_with_progress()


@app.post("/api/goals")
async def api_create_goal(payload: GoalCreate):
    if payload.metric not in ("revenue", "count"):
        raise HTTPException(status_code=400, detail="Métrica inválida. Usá 'revenue' o 'count'.")
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO goals (name, platform, event_name, metric, target_value)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                payload.name,
                payload.platform,
                payload.event_name or None,
                payload.metric,
                payload.target_value,
            ),
        )
    evaluate_goals()
    return {"ok": True}


@app.delete("/api/goals/{goal_id}")
async def api_delete_goal(goal_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM goals WHERE id = ?", (goal_id,))
    return {"ok": True}


@app.get("/api/alerts")
async def api_get_alerts(limit: int = Query(30, ge=1, le=100)):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        unread = conn.execute("SELECT COUNT(*) AS c FROM alerts WHERE is_read = 0").fetchone()["c"]
    return {"alerts": [dict(r) for r in rows], "unread_count": unread}


@app.post("/api/alerts/read-all")
async def api_read_all_alerts():
    with get_conn() as conn:
        conn.execute("UPDATE alerts SET is_read = 1 WHERE is_read = 0")
    return {"ok": True}


@app.get("/api/sync/status")
async def api_sync_status():
    cfg = load_config()
    lume_connected = get_scraper("lumepic").has_session()
    mom_connected = get_scraper("momentia").has_session()
    with get_conn() as conn:
        logs = conn.execute(
            "SELECT * FROM sync_logs ORDER BY id DESC LIMIT 12"
        ).fetchall()
    return {
        "config": cfg,
        "scheduler": get_scheduler_status(),
        "sessions": {
            "lumepic": lume_connected,
            "momentia": mom_connected,
        },
        "logs": [dict(r) for r in logs],
    }


@app.post("/api/sync/now")
async def api_sync_now(platform: Optional[str] = Query(None)):
    targets = [platform] if platform in ("lumepic", "momentia") else None
    results = await sync_all_platforms(targets)
    return {"results": results}


_login_tasks: dict[str, asyncio.Task] = {}


@app.post("/api/connect/{platform}")
async def api_connect_platform(platform: str):
    if platform not in ("lumepic", "momentia"):
        raise HTTPException(status_code=400, detail="Plataforma inválida.")
    existing = _login_tasks.get(platform)
    if existing and not existing.done():
        return {
            "platform": platform,
            "started": True,
            "message": f"Ya hay una ventana de inicio de sesión abierta para {platform.capitalize()}.",
        }
    scraper = get_scraper(platform)
    _login_tasks[platform] = asyncio.create_task(scraper.interactive_login())
    return {
        "platform": platform,
        "started": True,
        "message": f"Abriendo navegador para iniciar sesión en {platform.capitalize()}. Cuando veas tu tablero, cerrá esa ventana para guardar la sesión.",
    }


@app.post("/api/disconnect/{platform}")
async def api_disconnect_platform(platform: str):
    if platform not in ("lumepic", "momentia"):
        raise HTTPException(status_code=400, detail="Plataforma inválida.")
    get_scraper(platform).clear_session()
    return {"platform": platform, "connected": False}


@app.post("/api/config")
async def api_update_config(payload: ConfigUpdate):
    updates = payload.model_dump(exclude_none=True)
    cfg = save_config(updates)
    if "active_sync_interval_minutes" in updates or "idle_sync_interval_minutes" in updates:
        reschedule_sync()
    return {"ok": True, "config": cfg}


@app.post("/api/demo/seed")
async def api_demo_seed():
    counts = seed_demo_data()
    return {"ok": True, "seeded": counts}


@app.post("/api/demo/clear")
async def api_demo_clear():
    clear_demo_data()
    return {"ok": True}
