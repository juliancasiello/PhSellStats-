import asyncio
import json
import random
import re
import traceback
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from playwright.async_api import BrowserContext, Page, Response, async_playwright

from app.config import CAPTURES_DIR, SESSIONS_DIR, save_config

LOCAL_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


def _find_brave_executable() -> Optional[str]:
    candidates = [
        Path(r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"),
        Path(r"C:\Program Files (x86)\BraveSoftware\Brave-Browser\Application\brave.exe"),
        Path.home() / r"AppData\Local\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    for path in candidates:
        if path.exists():
            return str(path)
    return None


class AuthRequiredError(Exception):
    """Raised when the platform requires interactive login or the session expired."""


class BasePlatformScraper(ABC):
    platform_name: str = "base"

    def __init__(self, login_url: str, dashboard_url: str) -> None:
        self.login_url = login_url
        self.dashboard_url = dashboard_url
        self.state_path: Path = SESSIONS_DIR / f"{self.platform_name}_state.json"
        self.capture_path: Path = CAPTURES_DIR / f"{self.platform_name}_network.json"

    def has_session(self) -> bool:
        if self.state_path.exists() and self.state_path.stat().st_size > 10:
            return True
        # Try restoring session from Turso/DB kv_store (critical for ephemeral cloud disks like Render)
        try:
            from app.database import kv_get
            saved = kv_get(f"session:{self.platform_name}")
            if saved and len(saved) > 10:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                self.state_path.write_text(saved, encoding="utf-8")
                return True
        except Exception:
            pass
        return False

    def clear_session(self) -> None:
        if self.state_path.exists():
            self.state_path.unlink()
        try:
            from app.database import kv_delete
            kv_delete(f"session:{self.platform_name}")
        except Exception:
            pass

    def _persist_session_to_db(self) -> None:
        try:
            if self.state_path.exists() and self.state_path.stat().st_size > 10:
                from app.database import kv_set
                kv_set(f"session:{self.platform_name}", self.state_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    async def interactive_login(self, timeout_seconds: int = 180) -> bool:
        """Open a visible Brave/Chromium window so the user can log in manually and save session cookies."""
        captured_apis: list[dict[str, Any]] = []
        brave_exe = _find_brave_executable()
        print(f"[PhSellStats] Iniciando login interactivo para {self.platform_name} (browser={brave_exe or 'chromium'})", flush=True)

        try:
            async with async_playwright() as p:
                launch_kwargs: dict[str, Any] = {
                    "headless": False,
                    "args": ["--new-window", "--start-maximized"],
                }
                if brave_exe:
                    launch_kwargs["executable_path"] = brave_exe
                browser = await p.chromium.launch(**launch_kwargs)
                context_kwargs: dict[str, Any] = {}
                if self.has_session():
                    context_kwargs["storage_state"] = str(self.state_path)
                context = await browser.new_context(**context_kwargs)
                page = await context.new_page()

                async def on_response(res: Response) -> None:
                    try:
                        ct = res.headers.get("content-type", "")
                        if "application/json" in ct and res.status == 200:
                            url = res.url
                            if any(k in url.lower() for k in ("sale", "order", "venta", "event", "album", "stat", "dashboard", "graphql")):
                                body = await res.json()
                                captured_apis.append({"url": url, "sample": body if isinstance(body, dict) else {"items_count": len(body)}})
                    except Exception:
                        pass

                page.on("response", on_response)
                await page.goto(self.login_url, wait_until="domcontentloaded")
                await page.bring_to_front()

                last_url = self.login_url

                def on_framenavigated(frame) -> None:
                    nonlocal last_url
                    if frame == page.main_frame and frame.url.startswith("http"):
                        last_url = frame.url

                page.on("framenavigated", on_framenavigated)

                # Wait until user closes the page/browser or timeout expires
                try:
                    await page.wait_for_event("close", timeout=timeout_seconds * 1000)
                except Exception:
                    pass

                try:
                    await context.storage_state(path=str(self.state_path))
                    self._persist_session_to_db()
                except Exception:
                    pass

                if captured_apis:
                    try:
                        self.capture_path.write_text(
                            json.dumps(captured_apis[-20:], indent=2, ensure_ascii=False, default=str),
                            encoding="utf-8",
                        )
                    except Exception:
                        pass

                await browser.close()
                return self.has_session()
        except Exception as exc:
            print(f"[PhSellStats] Error en interactive_login({self.platform_name}): {exc}", flush=True)
            traceback.print_exc()
            return False

    async def _run_with_context(self) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Load saved session headlessly, navigate to dashboard, collect JSON responses & DOM rows."""
        if not self.has_session():
            raise AuthRequiredError(
                f"No hay sesión guardada para {self.platform_name}. Hacé clic en 'Conectar cuenta' para iniciar sesión."
            )

        json_payloads: list[dict[str, Any]] = []
        brave_exe = _find_brave_executable()

        async with async_playwright() as p:
            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if brave_exe:
                launch_kwargs["executable_path"] = brave_exe
            browser = await p.chromium.launch(**launch_kwargs)
            context: BrowserContext = await browser.new_context(
                storage_state=str(self.state_path),
                viewport={"width": 1366, "height": 768},
                locale="es-AR",
                timezone_id="America/Argentina/Buenos_Aires",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            )
            await context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page: Page = await context.new_page()

            async def on_response(res: Response) -> None:
                try:
                    ct = res.headers.get("content-type", "")
                    if "application/json" in ct and res.status == 200:
                        data = await res.json()
                        json_payloads.append({"url": res.url, "data": data})
                except Exception:
                    pass

            page.on("response", on_response)

            await asyncio.sleep(random.uniform(1.0, 2.8))
            await page.goto(self.dashboard_url, wait_until="networkidle", timeout=35000)
            await asyncio.sleep(random.uniform(1.2, 2.5))
            current_url = page.url.lower()
            if any(tok in current_url for tok in ("login", "signin", "iniciar-sesion", "auth")):
                await browser.close()
                raise AuthRequiredError(
                    f"La sesión de {self.platform_name} expiró (redirigido a {page.url}). Volvé a conectar tu cuenta."
                )

            try:
                await page.mouse.wheel(0, random.randint(200, 500))
                await asyncio.sleep(random.uniform(0.8, 1.8))
            except Exception:
                pass

            # Allow subclass to navigate to specific sales tabs or extract DOM tables
            dom_rows = await self.extract_from_page(page, json_payloads)

            # Refresh saved storage state (keeps cookies fresh)
            await context.storage_state(path=str(self.state_path))
            self._persist_session_to_db()
            await browser.close()

        # Save latest captured API structure for debugging/inspection
        if json_payloads:
            try:
                summary = [{"url": x["url"], "keys": list(x["data"].keys()) if isinstance(x["data"], dict) else f"list[{len(x['data'])}]"} for x in json_payloads[-25:]]
                self.capture_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
            except Exception:
                pass

        return json_payloads, dom_rows

    @abstractmethod
    async def extract_from_page(
        self, page: Page, json_payloads: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Navigate within the authenticated dashboard and extract raw DOM rows if needed."""

    @abstractmethod
    async def fetch_sales(self) -> list[dict[str, Any]]:
        """Fetch and normalize sales records from the platform."""

    @staticmethod
    def parse_amount(raw_val: Any) -> float:
        if isinstance(raw_val, (int, float)):
            return float(raw_val)
        if not raw_val:
            return 0.0
        text = str(raw_val).strip()
        cleaned = re.sub(r"[^\d.,-]", "", text)
        if not cleaned:
            return 0.0
        if "," in cleaned and "." in cleaned:
            if cleaned.rfind(",") > cleaned.rfind("."):
                # Format: 110.180,50 (es-AR)
                cleaned = cleaned.replace(".", "").replace(",", ".")
            else:
                # Format: 110,180.50 (en-US)
                cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            parts = cleaned.split(",")
            if len(parts) == 2 and len(parts[-1]) in (1, 2):
                cleaned = cleaned.replace(",", ".")
            else:
                cleaned = cleaned.replace(",", "")
        elif "." in cleaned:
            parts = cleaned.split(".")
            # Format: 110.180 or 1.250.000 (es-AR thousands separator)
            if len(parts) > 2 or (len(parts) == 2 and len(parts[-1]) == 3):
                cleaned = cleaned.replace(".", "")
        try:
            return float(cleaned)
        except ValueError:
            return 0.0

    @staticmethod
    def parse_datetime(raw_dt: Any) -> str:
        if not raw_dt:
            return datetime.now(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="seconds")
        s = str(raw_dt).strip()
        # Handle ISO 8601 with Z or explicit timezone offset -> convert to LOCAL_TZ (UTC-3)
        if s.endswith("Z") or re.search(r"[+-]\d{2}:?\d{2}$", s):
            try:
                iso_s = s.replace("Z", "+00:00")
                dt = datetime.fromisoformat(iso_s)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="seconds")
            except ValueError:
                pass
        for fmt in (
            "%Y-%m-%dT%H:%M:%S.%f",
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d %H:%M:%S",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%d/%m/%Y",
            "%Y-%m-%d",
        ):
            try:
                return datetime.strptime(s[:26], fmt).isoformat(timespec="seconds")
            except ValueError:
                continue
        try:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is not None:
                dt = dt.astimezone(LOCAL_TZ).replace(tzinfo=None)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            return datetime.now(LOCAL_TZ).replace(tzinfo=None).isoformat(timespec="seconds")

    def normalize_json_records(self, json_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Generic heuristic extractor for JSON sales/orders arrays returned by SPA dashboards."""
        normalized: list[dict[str, Any]] = []
        seen_ids: set[str] = set()

        for payload in json_payloads:
            url = payload["url"].lower()
            if not any(k in url for k in ("sale", "order", "venta", "payment", "pago", "transaction", "purchase")):
                continue
            data = payload["data"]
            items: list[Any] = []
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                for key in ("data", "items", "sales", "orders", "ventas", "results", "records"):
                    if isinstance(data.get(key), list):
                        items = data[key]
                        break

            for idx, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                ext_id = str(
                    item.get("id")
                    or item.get("_id")
                    or item.get("order_id")
                    or item.get("orderId")
                    or item.get("code")
                    or f"{hash(json.dumps(item, sort_keys=True, default=str))}_{idx}"
                )
                if ext_id in seen_ids:
                    continue

                amount_raw = (
                    item.get("amount")
                    or item.get("total")
                    or item.get("total_amount")
                    or item.get("totalAmount")
                    or item.get("price")
                    or item.get("net_amount")
                )
                if amount_raw is None:
                    continue

                event_obj = item.get("event") or item.get("album") or item.get("gallery") or {}
                if isinstance(event_obj, dict):
                    event_name = event_obj.get("name") or event_obj.get("title") or "Evento sin nombre"
                    event_ext = str(event_obj.get("id") or event_name)
                else:
                    event_name = str(item.get("event_name") or item.get("eventName") or item.get("album_name") or "Evento sin nombre")
                    event_ext = event_name

                sold_raw = (
                    item.get("sold_at")
                    or item.get("created_at")
                    or item.get("createdAt")
                    or item.get("date")
                    or item.get("paid_at")
                )

                buyer = item.get("buyer_name") or item.get("customer_name") or item.get("client_name")
                if not buyer and isinstance(item.get("buyer"), dict):
                    buyer = item["buyer"].get("name") or item["buyer"].get("email")

                seen_ids.add(ext_id)
                normalized.append(
                    {
                        "external_id": ext_id,
                        "event_external_id": event_ext,
                        "event_name": event_name,
                        "sold_at": self.parse_datetime(sold_raw),
                        "amount": self.parse_amount(amount_raw),
                        "items_count": int(item.get("items_count") or item.get("photos_count") or item.get("quantity") or 1),
                        "buyer_name": buyer,
                        "status": str(item.get("status") or "completed").lower(),
                        "raw": item,
                    }
                )

        return normalized
