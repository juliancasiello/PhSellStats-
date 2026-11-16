import asyncio
import base64
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import APP_PASSWORD, BASE_DIR, load_config, save_config
from app.database import (
    clear_demo_data,
    evaluate_goals,
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


@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if APP_PASSWORD and request.url.path != "/healthz":
        auth = request.headers.get("Authorization", "")
        authorized = False
        if auth.startswith("Basic "):
            try:
                decoded = base64.b64decode(auth[6:]).decode("utf-8")
                _, pwd = decoded.split(":", 1)
                authorized = secrets.compare_digest(pwd, APP_PASSWORD)
            except Exception:
                authorized = False
        if not authorized:
            return Response(
                status_code=401,
                headers={"WWW-Authenticate": 'Basic realm="PhSellStats"'},
                content="Autenticación requerida",
            )
    return await call_next(request)


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


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


@app.get("/api/summary")
async def api_summary(
    platform: str = Query("all"),
    event_name: str = Query("all"),
    days: int = Query(30),
    date_mode: str = Query("sale_date", pattern="^(sale_date|event_date)$"),
):
    return get_summary(platform=platform, event_name=event_name, days=days, date_mode=date_mode)


@app.get("/api/timeseries")
async def api_timeseries(
    granularity: str = Query("hour", pattern="^(hour|day)$"),
    platform: str = Query("all"),
    event_name: str = Query("all"),
    days: int = Query(30),
    date_mode: str = Query("sale_date", pattern="^(sale_date|event_date)$"),
):
    return get_timeseries(
        granularity=granularity,
        platform=platform,
        event_name=event_name,
        days=days,
        date_mode=date_mode,
    )


@app.get("/api/events")
async def api_events(
    days: int = Query(90),
    platform: str = Query("all"),
    date_mode: str = Query("sale_date", pattern="^(sale_date|event_date)$"),
):
    comparison = get_events_comparison(days=days, platform=platform, date_mode=date_mode)
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
