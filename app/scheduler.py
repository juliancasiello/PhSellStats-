import asyncio
import random
import time
from datetime import datetime, timedelta
from typing import Any, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import load_config
from app.database import create_alert, evaluate_goals, has_recent_real_sales, log_sync, upsert_sales_batch
from app.scrapers.base import AuthRequiredError, BasePlatformScraper
from app.scrapers.lumepic import LumepicScraper
from app.scrapers.momentia import MomentiaScraper

scheduler = AsyncIOScheduler()
_sync_lock = asyncio.Lock()
_scheduler_state: dict[str, Any] = {
    "mode": "idle",
    "interval_minutes": 120,
    "next_sync_at": None,
}


def get_scraper(platform: str) -> BasePlatformScraper:
    cfg = load_config()
    if platform == "lumepic":
        return LumepicScraper(
            login_url=cfg["lumepic_login_url"],
            dashboard_url=cfg["lumepic_dashboard_url"],
        )
    if platform == "momentia":
        return MomentiaScraper(
            login_url=cfg["momentia_login_url"],
            dashboard_url=cfg["momentia_dashboard_url"],
        )
    raise ValueError(f"Plataforma desconocida: {platform}")


async def sync_platform(platform: str) -> dict[str, Any]:
    scraper = get_scraper(platform)
    start = time.perf_counter()
    try:
        sales_items = await scraper.fetch_sales()
        res = upsert_sales_batch(platform, sales_items, is_demo=0)
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        new_count = res["new_count"]
        log_sync(platform, "success", new_count, f"Sincronizadas {len(sales_items)} ventas ({new_count} nuevas).", elapsed_ms)

        if new_count > 0:
            total_new_rev = sum(s["amount"] for s in res["new_sales"])
            create_alert(
                "new_sale",
                f"¡{new_count} nueva(s) venta(s) en {platform.capitalize()}!",
                f"Ingresaron {new_count} ventas por un total de ${total_new_rev:,.0f}.",
                platform=platform,
            )

        achieved = evaluate_goals()
        for g in achieved:
            create_alert(
                "goal_reached",
                f"Meta alcanzada: {g['name']}",
                f"Se alcanzó el objetivo ({g['current']:,.0f} / {g['target']:,.0f}).",
                platform=g["platform"],
            )

        return {"platform": platform, "status": "success", "total_fetched": len(sales_items), "new_sales": new_count}

    except AuthRequiredError as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        msg = str(exc)
        log_sync(platform, "auth_required", 0, msg, elapsed_ms)
        create_alert(
            "sync_error",
            f"Sesión requerida en {platform.capitalize()}",
            msg,
            platform=platform,
        )
        return {"platform": platform, "status": "auth_required", "message": msg}

    except Exception as exc:
        elapsed_ms = int((time.perf_counter() - start) * 1000)
        msg = f"Error al sincronizar {platform}: {exc}"
        log_sync(platform, "error", 0, msg, elapsed_ms)
        create_alert(
            "sync_error",
            f"Error de sincronización ({platform.capitalize()})",
            msg,
            platform=platform,
        )
        return {"platform": platform, "status": "error", "message": msg}


async def sync_all_platforms(platforms: Optional[list[str]] = None) -> list[dict[str, Any]]:
    if _sync_lock.locked():
        return [{"status": "skipped", "message": "Ya hay una sincronización en curso."}]

    async with _sync_lock:
        cfg = load_config()
        targets = platforms or []
        if not targets:
            if cfg.get("lumepic_enabled", True):
                targets.append("lumepic")
            if cfg.get("momentia_enabled", True):
                targets.append("momentia")

        results = []
        for idx, plat in enumerate(targets):
            if idx > 0:
                # Random human-like pause between platforms
                await asyncio.sleep(random.uniform(4.0, 12.0))
            results.append(await sync_platform(plat))

        any_new = any(r.get("new_sales", 0) > 0 for r in results)
        schedule_next_sync(force_active=any_new)
        return results


def _compute_next_delay_minutes(force_active: bool = False) -> tuple[str, float]:
    cfg = load_config()
    active_min = max(int(cfg.get("active_sync_interval_minutes", 10)), 1)
    idle_min = max(int(cfg.get("idle_sync_interval_minutes", 120)), 5)

    is_active = force_active or has_recent_real_sales(hours=3)
    if is_active:
        # ~10 min with +/- 1.5 min human jitter
        jitter = random.uniform(-1.5, 1.5)
        return "active", max(active_min + jitter, 2.0)
    else:
        # ~120 min with +/- 8 min human jitter
        jitter = random.uniform(-8.0, 8.0)
        return "idle", max(idle_min + jitter, 5.0)


def schedule_next_sync(force_active: bool = False) -> None:
    if not scheduler.running:
        return
    mode, delay_min = _compute_next_delay_minutes(force_active=force_active)
    run_at = datetime.now() + timedelta(minutes=delay_min)
    _scheduler_state["mode"] = mode
    _scheduler_state["interval_minutes"] = round(delay_min, 1)
    _scheduler_state["next_sync_at"] = run_at.isoformat(timespec="seconds")

    scheduler.add_job(
        sync_all_platforms,
        "date",
        run_date=run_at,
        id="periodic_sales_sync",
        replace_existing=True,
    )


def get_scheduler_status() -> dict[str, Any]:
    return _scheduler_state.copy()


def start_scheduler() -> None:
    if not scheduler.running:
        scheduler.start()
        schedule_next_sync(force_active=False)


def reschedule_sync() -> None:
    schedule_next_sync(force_active=False)


def stop_scheduler() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
