import asyncio
import random
from typing import Any
from playwright.async_api import Page

from app.scrapers.base import BasePlatformScraper


class MomentiaScraper(BasePlatformScraper):
    platform_name = "momentia"

    async def extract_from_page(
        self, page: Page, json_payloads: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        # Check total remote vs local Momentia count & existing IDs
        total_remote = 0
        has_more = False
        first_page_ids: list[str] = []
        for p in json_payloads:
            if "/api/photographer/sales" in p["url"].lower() and isinstance(p["data"], dict):
                total_remote = int(p["data"].get("total") or 0)
                has_more = bool(p["data"].get("has_more"))
                first_page_ids = [
                    str(o["order_id"])
                    for o in (p["data"].get("orders") or [])
                    if isinstance(o, dict) and o.get("order_id")
                ]
                break

        if has_more and total_remote > 0:
            from app.database import get_conn
            with get_conn() as conn:
                rows = conn.execute(
                    "SELECT external_id FROM sales WHERE platform = 'momentia' AND is_demo = 0"
                ).fetchall()
                existing_ids = {r["external_id"] for r in rows}

            is_backfill = len(existing_ids) < total_remote
            max_clicks = 45 if is_backfill else 5

            # On incremental sync, check if page 1 already has all known orders
            all_known_on_first = bool(first_page_ids) and all(oid in existing_ids for oid in first_page_ids)
            if is_backfill or not all_known_on_first:
                for _ in range(max_clicks):
                    try:
                        btn = page.locator("button:has-text('Cargar más')").first
                        if not await btn.is_visible(timeout=2000):
                            break
                        await btn.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(0.35, 0.75))
                        async with page.expect_response(
                            lambda r: "/api/photographer/sales" in r.url and r.status == 200,
                            timeout=12000,
                        ) as resp_info:
                            await btn.click()
                        resp = await resp_info.value
                        body = await resp.json()
                        if not isinstance(body, dict):
                            break
                        page_orders = body.get("orders") or []
                        page_ids = [str(o["order_id"]) for o in page_orders if isinstance(o, dict) and o.get("order_id")]
                        # Wait briefly for any detail state to settle
                        await asyncio.sleep(0.15)
                        if not body.get("has_more"):
                            break
                        # On incremental sync, stop once an entire page is already in SQLite
                        if not is_backfill and page_ids and all(oid in existing_ids for oid in page_ids):
                            break
                    except Exception:
                        break

        return []

    def _parse_sales_orders(self, json_payloads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Parse individual orders from Momentia's /api/photographer/sales endpoint."""
        by_order_id: dict[str, dict[str, Any]] = {}
        for payload in json_payloads:
            if "/api/photographer/sales" not in payload["url"].lower():
                continue
            data = payload["data"]
            if not isinstance(data, dict):
                continue
            orders = data.get("orders")
            if not isinstance(orders, list):
                continue
            for o in orders:
                if not isinstance(o, dict):
                    continue
                order_id = o.get("order_id") or o.get("id")
                if not order_id:
                    continue
                ext_id = str(order_id)
                ev_name = str(o.get("event_name") or "Momentia Evento").strip()
                ev_id = str(o.get("event_id") or ev_name)
                sold_at = self.parse_datetime(o.get("completed_at") or o.get("created_at"))
                amount_raw = o.get("my_earnings")
                if amount_raw is None:
                    amount_raw = o.get("my_revenue") or o.get("order_total_amount") or 0
                amount = self.parse_amount(amount_raw)
                items_count = int(o.get("my_photos") or o.get("order_total_photos") or 1)

                by_order_id[ext_id] = {
                    "external_id": ext_id,
                    "event_external_id": ev_id,
                    "event_name": ev_name,
                    "event_date": sold_at[:10],
                    "sold_at": sold_at,
                    "amount": amount,
                    "items_count": max(items_count, 1),
                    "buyer_name": None,
                    "status": "completed",
                    "raw": o,
                }
        return list(by_order_id.values())

    async def fetch_sales(self) -> list[dict[str, Any]]:
        json_payloads, _ = await self._run_with_context()

        orders = self._parse_sales_orders(json_payloads)
        if orders:
            return orders

        # Fallback generic JSON extractor if endpoint changed
        return self.normalize_json_records(json_payloads)
