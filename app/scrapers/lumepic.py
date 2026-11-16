import asyncio
import random
from typing import Any
from playwright.async_api import Page

from app.scrapers.base import BasePlatformScraper


class LumepicScraper(BasePlatformScraper):
    platform_name = "lumepic"

    async def extract_from_page(
        self, page: Page, json_payloads: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        # Gently scroll down to trigger infinite-scroll sales if more exist
        from app.database import get_conn
        with get_conn() as conn:
            existing_ids = {
                r["external_id"]
                for r in conn.execute("SELECT external_id FROM sales WHERE platform = 'lumepic' AND is_demo = 0").fetchall()
            }

        is_backfill = len(existing_ids) < 30
        max_scrolls = 12 if is_backfill else 2
        for _ in range(max_scrolls):
            before = len(json_payloads)
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(random.uniform(1.0, 1.8))
                await page.wait_for_load_state("networkidle", timeout=4000)
            except Exception:
                pass
            if len(json_payloads) == before:
                break

        rows_data: list[dict[str, Any]] = []
        try:
            trs = page.locator("table tbody tr")
            count = await trs.count()
            for i in range(min(count, 200)):
                cells = [c.strip() for c in await trs.nth(i).locator("td").all_inner_texts()]
                if len(cells) >= 3:
                    rows_data.append({"cells": cells})
        except Exception:
            pass
        return rows_data

    def _parse_lumepic_sale_obj(self, item: dict[str, Any]) -> dict[str, Any] | None:
        ext_id = item.get("id")
        if not ext_id:
            return None

        # Calculate photographer's own gross from lineItems (in case cart has multiple photographers)
        line_items = item.get("lineItems") if isinstance(item.get("lineItems"), list) else []
        my_line_ids: set[str] = set()
        my_gross = 0.0
        for li in line_items:
            if not isinstance(li, dict):
                continue
            if li.get("id"):
                my_line_ids.add(str(li["id"]))
            li_price = li.get("totalPrice")
            if li_price is None:
                li_price = li.get("subtotalPrice")
            if li_price is not None:
                my_gross += self.parse_amount(li_price)

        if my_gross <= 0:
            amount_raw = item.get("totalPrice")
            if amount_raw is None:
                amount_raw = item.get("value")
            my_gross = self.parse_amount(amount_raw)

        # Subtract platform/payment fees for photographer's lineItems -> exact net earnings
        fees_ars = 0.0
        for pay in item.get("payments") or []:
            if not isinstance(pay, dict):
                continue
            rate = pay.get("officialExchangeRate") or 0
            for fee in pay.get("fees") or []:
                if not isinstance(fee, dict):
                    continue
                fee_li = fee.get("lineItemId")
                if my_line_ids and fee_li and str(fee_li) not in my_line_ids:
                    continue
                f_amt = float(fee.get("amount") or 0)
                f_cur = str(fee.get("currency") or "").lower()
                if f_cur == "usd" and rate > 0:
                    fees_ars += f_amt / float(rate)
                elif f_cur == "ars":
                    fees_ars += f_amt

        amount = round(max(my_gross - fees_ars, 0.0), 2)

        # Extract event name, date, and photo count from groupedLineItems or lineItems
        event_name = "Lumepic Venta"
        event_ext = "lumepic_general"
        event_date = None
        items_count = 0

        for key in ("groupedLineItems", "lineItems"):
            lines = item.get(key)
            if isinstance(lines, list) and lines:
                items_count = 0
                for li in lines:
                    if not isinstance(li, dict):
                        continue
                    meta = li.get("metadata") if isinstance(li.get("metadata"), dict) else {}
                    qty = meta.get("quantityInPackage") or li.get("quantity") or li.get("count") or 1
                    try:
                        items_count += int(qty)
                    except (TypeError, ValueError):
                        items_count += 1
                    ev = li.get("album") or li.get("event") or {}
                    if isinstance(ev, dict) and (ev.get("description") or ev.get("name") or ev.get("title")):
                        event_name = str(ev.get("description") or ev.get("name") or ev.get("title")).strip()
                        event_ext = str(ev.get("id") or event_name)
                        if ev.get("takenDate"):
                            event_date = self.parse_datetime(ev["takenDate"])[:10]
                    elif li.get("eventName") or li.get("albumName") or li.get("title"):
                        event_name = str(li.get("eventName") or li.get("albumName") or li.get("title")).strip()
                        event_ext = event_name
                if event_name != "Lumepic Venta":
                    break

        if items_count <= 0:
            items_count = 1

        return {
            "external_id": str(ext_id),
            "event_external_id": event_ext,
            "event_name": event_name,
            "event_date": event_date,
            "sold_at": self.parse_datetime(item.get("createdAt")),
            "amount": amount,
            "items_count": items_count,
            "buyer_name": None,
            "status": str(item.get("status") or "completed").lower(),
            "raw": item,
        }

    async def fetch_sales(self) -> list[dict[str, Any]]:
        json_payloads, dom_rows = await self._run_with_context()

        # 1. Parse individual /api/sales/<uuid> payloads captured from Lumepic
        parsed_by_id: dict[str, dict[str, Any]] = {}
        for payload in json_payloads:
            url = payload["url"].lower()
            data = payload["data"]
            if "/api/sales/" in url and isinstance(data, dict) and "id" in data and "createdAt" in data:
                rec = self._parse_lumepic_sale_obj(data)
                if rec:
                    parsed_by_id[rec["external_id"]] = rec

        if parsed_by_id:
            return list(parsed_by_id.values())

        # 2. Fallback to generic JSON list extractor
        sales = self.normalize_json_records(json_payloads)
        if sales:
            return sales

        # 3. Fallback DOM table parser
        parsed: list[dict[str, Any]] = []
        for idx, row in enumerate(dom_rows):
            cells = row["cells"]
            amount = 0.0
            sold_at = None
            event_name = cells[0].splitlines()[0].strip() if cells else "Lumepic Evento"
            for c in cells:
                if "$" in c or "ARS" in c or "USD" in c:
                    val = self.parse_amount(c)
                    if val > 0:
                        amount = val
                elif "/" in c or "-" in c:
                    if any(ch.isdigit() for ch in c):
                        sold_at = self.parse_datetime(c)
            if amount > 0:
                parsed.append(
                    {
                        "external_id": f"dom_lumepic_{idx}_{sold_at or ''}_{amount}",
                        "event_external_id": event_name,
                        "event_name": event_name,
                        "sold_at": sold_at or self.parse_datetime(None),
                        "amount": amount,
                        "items_count": 1,
                        "buyer_name": None,
                        "status": "completed",
                        "raw": {"cells": cells},
                    }
                )
        return parsed
