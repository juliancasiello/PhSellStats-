import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Generator, Optional

from app.config import DB_PATH, TURSO_AUTH_TOKEN, TURSO_DATABASE_URL
from app.turso_client import TursoConnection


def is_turso_enabled() -> bool:
    return bool(TURSO_DATABASE_URL and TURSO_AUTH_TOKEN)


@contextmanager
def get_conn() -> Generator[Any, None, None]:
    if is_turso_enabled():
        conn = TursoConnection(TURSO_DATABASE_URL, TURSO_AUTH_TOKEN)
    else:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                name TEXT NOT NULL,
                canonical_name TEXT,
                event_date TEXT,
                is_demo INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(platform, external_id)
            );

            CREATE TABLE IF NOT EXISTS sales (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                external_id TEXT NOT NULL,
                event_id INTEGER REFERENCES events(id) ON DELETE SET NULL,
                event_name TEXT NOT NULL,
                sold_at TEXT NOT NULL,
                amount REAL NOT NULL,
                items_count INTEGER DEFAULT 1,
                buyer_name TEXT,
                status TEXT DEFAULT 'completed',
                is_demo INTEGER DEFAULT 0,
                raw_json TEXT,
                synced_at TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(platform, external_id)
            );

            CREATE INDEX IF NOT EXISTS idx_sales_sold_at ON sales(sold_at);
            CREATE INDEX IF NOT EXISTS idx_sales_platform ON sales(platform);
            CREATE INDEX IF NOT EXISTS idx_sales_event ON sales(event_name);

            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                platform TEXT NOT NULL,
                total_sales INTEGER NOT NULL,
                total_revenue REAL NOT NULL,
                is_demo INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS goals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                platform TEXT DEFAULT 'all',
                event_name TEXT,
                metric TEXT NOT NULL CHECK(metric IN ('revenue', 'count')),
                target_value REAL NOT NULL,
                achieved INTEGER DEFAULT 0,
                achieved_at TEXT,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                alert_type TEXT NOT NULL,
                platform TEXT,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                is_read INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS sync_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                platform TEXT NOT NULL,
                status TEXT NOT NULL,
                new_sales_count INTEGER DEFAULT 0,
                message TEXT,
                duration_ms INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS kv_store (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT DEFAULT (datetime('now', 'localtime'))
            );
            """
        )
        # Safe migration for existing DBs
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(events)").fetchall()]
        if "canonical_name" not in cols:
            conn.execute("ALTER TABLE events ADD COLUMN canonical_name TEXT")
        auto_pair_events(conn)


_STOP_WORDS = {
    "de", "del", "la", "el", "los", "las", "y", "en", "con", "para", "por",
    "rosario", "buenos", "aires", "cordoba", "mendoza", "argentina",
}


def _normalize_event_tokens(name: str) -> set[str]:
    norm = unicodedata.normalize("NFKD", name.lower())
    ascii_str = "".join(ch for ch in norm if not unicodedata.combining(ch))
    words = re.findall(r"[a-z0-9]+", ascii_str)
    tokens = {w for w in words if w not in _STOP_WORDS and len(w) > 1}
    return tokens or set(words)


def auto_pair_events(conn: sqlite3.Connection) -> None:
    """
    1. Differentiate homonymous events on the SAME platform (e.g. 'Ciro y los persas (A)', '(B)').
    2. Pair matching events ACROSS Lumepic and Momentia under a shared canonical_name.
    """
    rows = conn.execute(
        "SELECT id, platform, external_id, name, event_date, is_demo FROM events ORDER BY COALESCE(event_date, '9999-99-99') ASC, id ASC"
    ).fetchall()
    if not rows:
        return

    # Step 1: Differentiate same-platform homonyms (same normalized tokens, different external_id)
    by_plat_tokens: dict[tuple[str, int, frozenset[str]], list[sqlite3.Row]] = {}
    for r in rows:
        tokens = frozenset(_normalize_event_tokens(r["name"]))
        by_plat_tokens.setdefault((r["platform"], r["is_demo"], tokens), []).append(r)

    display_name_by_id: dict[int, str] = {}
    for (_, _, _), group in by_plat_tokens.items():
        if len(group) > 1:
            for idx, r in enumerate(group):
                letter = chr(ord("A") + idx) if idx < 26 else str(idx + 1)
                base_name = re.sub(r"\s*\([A-Z0-9]+\)$", "", r["name"].strip())
                display_name_by_id[r["id"]] = f"{base_name} ({letter})"
        else:
            r = group[0]
            display_name_by_id[r["id"]] = r["name"].strip()

    # Step 2: Pair across different platforms (lumepic <-> momentia)
    clusters: list[dict[str, Any]] = []
    for r in rows:
        tokens = _normalize_event_tokens(r["name"])
        cand_name = display_name_by_id[r["id"]]
        matched_cluster = None
        for cl in clusters:
            if cl["is_demo"] != r["is_demo"]:
                continue
            # Never merge two different events from the SAME platform
            if r["platform"] in cl["platforms"]:
                continue
            ctokens = cl["tokens"]
            if not tokens or not ctokens:
                continue
            inter = len(tokens & ctokens)
            overlap = inter / min(len(tokens), len(ctokens))
            jaccard = inter / len(tokens | ctokens)
            if tokens == ctokens or (inter >= 2 and (overlap >= 0.75 or jaccard >= 0.6)):
                matched_cluster = cl
                break

        if matched_cluster is None:
            clusters.append(
                {
                    "is_demo": r["is_demo"],
                    "platforms": {r["platform"]},
                    "tokens": tokens,
                    "canonical_name": cand_name,
                    "ids": [r["id"]],
                }
            )
        else:
            matched_cluster["platforms"].add(r["platform"])
            matched_cluster["ids"].append(r["id"])
            if len(cand_name) < len(matched_cluster["canonical_name"]) and len(cand_name) >= 6:
                matched_cluster["canonical_name"] = cand_name

    for cl in clusters:
        placeholders = ",".join("?" for _ in cl["ids"])
        conn.execute(
            f"UPDATE events SET canonical_name = ? WHERE id IN ({placeholders})",
            [cl["canonical_name"], *cl["ids"]],
        )


def kv_get(key: str) -> Optional[str]:
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM kv_store WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row else None


def kv_set(key: str, value: str) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO kv_store (key, value, updated_at)
            VALUES (?, ?, datetime('now', 'localtime'))
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = datetime('now', 'localtime')
            """,
            (key, value),
        )


def kv_delete(key: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM kv_store WHERE key = ?", (key,))


def has_any_sales() -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT COUNT(*) AS c FROM sales").fetchone()
        return bool(row and row["c"] > 0)


def has_recent_real_sales(hours: int = 3) -> bool:
    """Return True if any non-demo sale occurred within the last `hours` hours."""
    cutoff = (datetime.now() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM sales WHERE is_demo = 0 AND sold_at >= ?",
            (cutoff,),
        ).fetchone()
        return bool(row and row["c"] > 0)


def upsert_event(
    conn: sqlite3.Connection,
    platform: str,
    external_id: str,
    name: str,
    event_date: Optional[str] = None,
    is_demo: int = 0,
) -> int:
    conn.execute(
        """
        INSERT INTO events (platform, external_id, name, event_date, is_demo)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(platform, external_id) DO UPDATE SET
            name = excluded.name,
            event_date = COALESCE(excluded.event_date, events.event_date)
        """,
        (platform, external_id, name, event_date, is_demo),
    )
    row = conn.execute(
        "SELECT id FROM events WHERE platform = ? AND external_id = ?",
        (platform, external_id),
    ).fetchone()
    return int(row["id"])


def upsert_sales_batch(
    platform: str,
    sales_items: list[dict[str, Any]],
    is_demo: int = 0,
) -> dict[str, Any]:
    """Insert or update a batch of sales and return counts of new sales."""
    new_sales: list[dict[str, Any]] = []
    with get_conn() as conn:
        for item in sales_items:
            ext_id = str(item["external_id"])
            existing = conn.execute(
                "SELECT id FROM sales WHERE platform = ? AND external_id = ?",
                (platform, ext_id),
            ).fetchone()

            event_ext_id = str(item.get("event_external_id") or item["event_name"])
            event_id = upsert_event(
                conn,
                platform=platform,
                external_id=event_ext_id,
                name=item["event_name"],
                event_date=item.get("event_date"),
                is_demo=is_demo,
            )

            raw_json = json.dumps(item.get("raw", {}), ensure_ascii=False)
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO sales (
                        platform, external_id, event_id, event_name,
                        sold_at, amount, items_count, buyer_name, status, is_demo, raw_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        platform,
                        ext_id,
                        event_id,
                        item["event_name"],
                        item["sold_at"],
                        float(item["amount"]),
                        int(item.get("items_count", 1)),
                        item.get("buyer_name"),
                        item.get("status", "completed"),
                        is_demo,
                        raw_json,
                    ),
                )
                new_sales.append(item)
            else:
                conn.execute(
                    """
                    UPDATE sales SET
                        event_id = ?, event_name = ?, sold_at = ?, amount = ?,
                        items_count = ?, buyer_name = ?, status = ?, raw_json = ?,
                        synced_at = datetime('now', 'localtime')
                    WHERE id = ?
                    """,
                    (
                        event_id,
                        item["event_name"],
                        item["sold_at"],
                        float(item["amount"]),
                        int(item.get("items_count", 1)),
                        item.get("buyer_name"),
                        item.get("status", "completed"),
                        raw_json,
                        existing["id"],
                    ),
                )

        # Re-run automatic cross-platform event pairing
        auto_pair_events(conn)

        # Record snapshot after sync
        totals = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS rev FROM sales WHERE platform = ?",
            (platform,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO snapshots (captured_at, platform, total_sales, total_revenue, is_demo)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                datetime.now().isoformat(timespec="seconds"),
                platform,
                int(totals["cnt"]),
                float(totals["rev"]),
                is_demo,
            ),
        )

    return {"new_count": len(new_sales), "new_sales": new_sales}


def create_alert(
    alert_type: str,
    title: str,
    message: str,
    platform: Optional[str] = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO alerts (alert_type, platform, title, message) VALUES (?, ?, ?, ?)",
            (alert_type, platform, title, message),
        )


def log_sync(
    platform: str,
    status: str,
    new_sales_count: int = 0,
    message: str = "",
    duration_ms: int = 0,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sync_logs (platform, status, new_sales_count, message, duration_ms)
            VALUES (?, ?, ?, ?, ?)
            """,
            (platform, status, new_sales_count, message, duration_ms),
        )


def evaluate_goals() -> list[dict[str, Any]]:
    """Evaluate all goals against current sales and return newly achieved ones."""
    newly_achieved: list[dict[str, Any]] = []
    with get_conn() as conn:
        goals = conn.execute("SELECT * FROM goals ORDER BY created_at DESC").fetchall()
        for g in goals:
            where_clauses = ["status != 'refunded'"]
            params: list[Any] = []
            if g["platform"] and g["platform"] != "all":
                where_clauses.append("platform = ?")
                params.append(g["platform"])
            if g["event_name"]:
                where_clauses.append("event_name = ?")
                params.append(g["event_name"])

            where_sql = " AND ".join(where_clauses)
            agg = conn.execute(
                f"SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS rev FROM sales WHERE {where_sql}",
                params,
            ).fetchone()
            current = float(agg["rev"] if g["metric"] == "revenue" else agg["cnt"])

            if not g["achieved"] and current >= float(g["target_value"]):
                now_str = datetime.now().isoformat(timespec="seconds")
                conn.execute(
                    "UPDATE goals SET achieved = 1, achieved_at = ? WHERE id = ?",
                    (now_str, g["id"]),
                )
                newly_achieved.append(
                    {
                        "id": g["id"],
                        "name": g["name"],
                        "platform": g["platform"],
                        "current": current,
                        "target": g["target_value"],
                    }
                )
            elif g["achieved"] and current < float(g["target_value"]):
                conn.execute(
                    "UPDATE goals SET achieved = 0, achieved_at = NULL WHERE id = ?",
                    (g["id"],),
                )
    return newly_achieved


def clear_demo_data() -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM sales WHERE is_demo = 1")
        conn.execute("DELETE FROM events WHERE is_demo = 1")
        conn.execute("DELETE FROM snapshots WHERE is_demo = 1")
        conn.execute("DELETE FROM alerts WHERE title LIKE '%[DEMO]%' OR message LIKE '%[DEMO]%'")
    evaluate_goals()


def _build_sales_filter(
    platform: str = "all",
    event_name: str = "all",
    days: int = 30,
    date_mode: str = "sale_date",
    alias: str = "",
    month: str = "all",
) -> tuple[str, list[Any]]:
    p = f"{alias}." if alias else ""
    clauses = [f"{p}status != 'refunded'"]
    params: list[Any] = []
    if platform != "all":
        clauses.append(f"{p}platform = ?")
        params.append(platform)
    if event_name != "all":
        clauses.append(
            f"({p}event_name = ? OR {p}event_id IN (SELECT id FROM events WHERE canonical_name = ?))"
        )
        params.extend([event_name, event_name])
    if month and month != "all":
        if date_mode == "event_date":
            clauses.append(
                f"substr(COALESCE((SELECT event_date FROM events WHERE id = {p}event_id), substr({p}sold_at, 1, 10)), 1, 7) = ?"
            )
            params.append(month)
        else:
            clauses.append(f"substr({p}sold_at, 1, 7) = ?")
            params.append(month)
    elif days > 0:
        if date_mode == "event_date":
            cutoff_date = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
            clauses.append(
                f"COALESCE((SELECT event_date FROM events WHERE id = {p}event_id), substr({p}sold_at, 1, 10)) >= ?"
            )
            params.append(cutoff_date)
        else:
            cutoff = (datetime.now() - timedelta(days=days)).isoformat(timespec="seconds")
            clauses.append(f"{p}sold_at >= ?")
            params.append(cutoff)
    return " AND ".join(clauses), params


def get_available_months() -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT substr(sold_at, 1, 7) AS m FROM sales WHERE status != 'refunded'
            UNION
            SELECT DISTINCT substr(event_date, 1, 7) AS m FROM events WHERE event_date IS NOT NULL
            ORDER BY m DESC
            """
        ).fetchall()
    return [str(r["m"]) for r in rows if r["m"]]


def get_summary(
    platform: str = "all",
    event_name: str = "all",
    days: int = 30,
    date_mode: str = "sale_date",
    month: str = "all",
) -> dict[str, Any]:
    where_sql, params = _build_sales_filter(platform, event_name, days, date_mode, month=month)
    with get_conn() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total_sales,
                COALESCE(SUM(amount), 0) AS total_revenue,
                COALESCE(SUM(items_count), 0) AS total_items,
                COALESCE(AVG(amount), 0) AS avg_ticket,
                MIN(sold_at) AS first_sale,
                MAX(sold_at) AS last_sale
            FROM sales WHERE {where_sql}
            """,
            params,
        ).fetchone()

        by_platform = conn.execute(
            f"""
            SELECT
                platform,
                COUNT(*) AS sales_count,
                COALESCE(SUM(amount), 0) AS revenue,
                COALESCE(SUM(items_count), 0) AS items_count
            FROM sales WHERE {where_sql}
            GROUP BY platform
            """,
            params,
        ).fetchall()

        # Velocity: sales in last 24h and last 1h
        now = datetime.now()
        cutoff_24h = (now - timedelta(hours=24)).isoformat(timespec="seconds")
        cutoff_1h = (now - timedelta(hours=1)).isoformat(timespec="seconds")
        vel_where, vel_params = _build_sales_filter(platform, event_name, 0)
        v24 = conn.execute(
            f"SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS rev FROM sales WHERE {vel_where} AND sold_at >= ?",
            [*vel_params, cutoff_24h],
        ).fetchone()
        v1 = conn.execute(
            f"SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS rev FROM sales WHERE {vel_where} AND sold_at >= ?",
            [*vel_params, cutoff_1h],
        ).fetchone()

        demo_row = conn.execute("SELECT COUNT(*) AS cnt FROM sales WHERE is_demo = 1").fetchone()

    return {
        "total_sales": int(row["total_sales"]),
        "total_revenue": round(float(row["total_revenue"]), 2),
        "total_items": int(row["total_items"]),
        "avg_ticket": round(float(row["avg_ticket"]), 2),
        "velocity_24h_per_hour": round(int(v24["cnt"]) / 24.0, 2),
        "sales_last_24h": int(v24["cnt"]),
        "revenue_last_24h": round(float(v24["rev"]), 2),
        "sales_last_1h": int(v1["cnt"]),
        "has_demo_data": bool(demo_row and demo_row["cnt"] > 0),
        "by_platform": [dict(r) for r in by_platform],
    }


def get_timeseries(
    granularity: str = "hour",
    platform: str = "all",
    event_name: str = "all",
    days: int = 30,
    date_mode: str = "sale_date",
    month: str = "all",
) -> list[dict[str, Any]]:
    where_sql, params = _build_sales_filter(platform, event_name, days, date_mode, month=month)
    fmt = "%Y-%m-%d %H:00" if granularity == "hour" else ("%Y-%m" if granularity == "month" else "%Y-%m-%d")
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
                strftime('{fmt}', sold_at) AS bucket,
                platform,
                COUNT(*) AS sales_count,
                COALESCE(SUM(items_count), 0) AS items_count,
                COALESCE(SUM(amount), 0) AS revenue
            FROM sales
            WHERE {where_sql}
            GROUP BY bucket, platform
            ORDER BY bucket ASC
            """,
            params,
        ).fetchall()

    buckets: dict[str, dict[str, Any]] = {}
    for r in rows:
        b = r["bucket"]
        if b not in buckets:
            buckets[b] = {
                "bucket": b,
                "lumepic_sales": 0,
                "lumepic_items": 0,
                "lumepic_revenue": 0.0,
                "momentia_sales": 0,
                "momentia_items": 0,
                "momentia_revenue": 0.0,
                "total_sales": 0,
                "total_items": 0,
                "total_revenue": 0.0,
            }
        plat = r["platform"]
        cnt = int(r["sales_count"])
        items = int(r["items_count"])
        rev = round(float(r["revenue"]), 2)
        if plat == "lumepic":
            buckets[b]["lumepic_sales"] += cnt
            buckets[b]["lumepic_items"] += items
            buckets[b]["lumepic_revenue"] = round(buckets[b]["lumepic_revenue"] + rev, 2)
        elif plat == "momentia":
            buckets[b]["momentia_sales"] += cnt
            buckets[b]["momentia_items"] += items
            buckets[b]["momentia_revenue"] = round(buckets[b]["momentia_revenue"] + rev, 2)
        else:
            buckets[b]["canon_sales"] = buckets[b].get("canon_sales", 0) + cnt
            buckets[b]["canon_revenue"] = round(buckets[b].get("canon_revenue", 0.0) + rev, 2)
        buckets[b]["total_sales"] += cnt
        buckets[b]["total_items"] += items
        buckets[b]["total_revenue"] = round(buckets[b]["total_revenue"] + rev, 2)

    if granularity == "month":
        if not buckets:
            return []
        sorted_m = sorted(buckets.keys())
        cur_y, cur_m = map(int, sorted_m[0].split("-"))
        end_dt_now = datetime.now()
        end_y, end_m = end_dt_now.year, end_dt_now.month
        last_y, last_m = map(int, sorted_m[-1].split("-"))
        if (last_y, last_m) > (end_y, end_m):
            end_y, end_m = last_y, last_m
        filled_m: list[dict[str, Any]] = []
        while (cur_y, cur_m) <= (end_y, end_m):
            key = f"{cur_y:04d}-{cur_m:02d}"
            filled_m.append(
                buckets.get(
                    key,
                    {
                        "bucket": key,
                        "lumepic_sales": 0,
                        "lumepic_items": 0,
                        "lumepic_revenue": 0.0,
                        "momentia_sales": 0,
                        "momentia_items": 0,
                        "momentia_revenue": 0.0,
                        "total_sales": 0,
                        "total_items": 0,
                        "total_revenue": 0.0,
                    },
                )
            )
            cur_m += 1
            if cur_m > 12:
                cur_m = 1
                cur_y += 1
        return filled_m

    step = timedelta(hours=1) if granularity == "hour" else timedelta(days=1)
    str_fmt = "%Y-%m-%d %H:00" if granularity == "hour" else "%Y-%m-%d"
    end_dt = datetime.now().replace(minute=0, second=0, microsecond=0)
    if granularity == "day":
        end_dt = end_dt.replace(hour=0)

    if month and month != "all":
        m_start = datetime.strptime(f"{month}-01", "%Y-%m-%d")
        if m_start.month == 12:
            m_end = datetime(m_start.year + 1, 1, 1) - timedelta(days=1)
        else:
            m_end = datetime(m_start.year, m_start.month + 1, 1) - timedelta(days=1)
        if granularity == "hour":
            m_end = m_end.replace(hour=23)
        cur = m_start
        if m_end < end_dt:
            end_dt = m_end
    elif days > 0 and days < 365:
        cur = end_dt - timedelta(days=days)
        if granularity == "hour":
            cur += timedelta(hours=1)
    elif buckets:
        cur = datetime.strptime(min(buckets.keys()), str_fmt)
    else:
        return []

    if buckets:
        last_data_dt = datetime.strptime(max(buckets.keys()), str_fmt)
        if last_data_dt > end_dt:
            end_dt = last_data_dt

    filled: list[dict[str, Any]] = []
    while cur <= end_dt:
        key = cur.strftime(str_fmt)
        if key in buckets:
            filled.append(buckets[key])
        else:
            filled.append(
                {
                    "bucket": key,
                    "lumepic_sales": 0,
                    "lumepic_items": 0,
                    "lumepic_revenue": 0.0,
                    "momentia_sales": 0,
                    "momentia_items": 0,
                    "momentia_revenue": 0.0,
                    "total_sales": 0,
                    "total_items": 0,
                    "total_revenue": 0.0,
                }
            )
        cur += step

    return filled


def get_events_comparison(
    days: int = 90,
    platform: str = "all",
    date_mode: str = "sale_date",
    month: str = "all",
) -> list[dict[str, Any]]:
    s_where, params = _build_sales_filter(platform, "all", days, date_mode, alias="s", month=month)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT
                COALESCE(e.canonical_name, s.event_name) AS event_name,
                s.platform,
                COUNT(*) AS sales_count,
                COALESCE(SUM(s.amount), 0) AS revenue,
                COALESCE(SUM(s.items_count), 0) AS items_count,
                COALESCE(AVG(s.amount), 0) AS avg_ticket,
                MIN(s.sold_at) AS first_sale,
                MAX(s.sold_at) AS last_sale
            FROM sales s
            LEFT JOIN events e ON e.id = s.event_id
            WHERE {s_where}
            GROUP BY COALESCE(e.canonical_name, s.event_name), s.platform
            ORDER BY revenue DESC
            """,
            params,
        ).fetchall()

    platforms_by_event: dict[str, set[str]] = {}
    for r in rows:
        if r["platform"] in ("lumepic", "momentia"):
            platforms_by_event.setdefault(r["event_name"], set()).add(r["platform"])

    result = []
    for r in rows:
        item = dict(r)
        item["paired"] = len(platforms_by_event.get(item["event_name"], set())) > 1
        try:
            t0 = datetime.fromisoformat(r["first_sale"])
            t1 = datetime.fromisoformat(r["last_sale"])
            hours = max((t1 - t0).total_seconds() / 3600.0, 1.0)
            item["velocity_per_hour"] = round(int(r["sales_count"]) / hours, 2)
        except Exception:
            item["velocity_per_hour"] = 0.0
        item["revenue"] = round(float(item["revenue"]), 2)
        item["avg_ticket"] = round(float(item["avg_ticket"]), 2)
        result.append(item)
    return result


def get_recent_sales(
    platform: str = "all",
    event_name: str = "all",
    limit: int = 50,
) -> list[dict[str, Any]]:
    where_sql, params = _build_sales_filter(platform, event_name, 0)
    with get_conn() as conn:
        rows = conn.execute(
            f"""
            SELECT id, platform, external_id, event_name, sold_at, amount, items_count, buyer_name, status, is_demo
            FROM sales
            WHERE {where_sql}
            ORDER BY sold_at DESC
            LIMIT ?
            """,
            [*params, limit],
        ).fetchall()
    return [dict(r) for r in rows]


def get_goals_with_progress() -> list[dict[str, Any]]:
    with get_conn() as conn:
        goals = conn.execute("SELECT * FROM goals ORDER BY achieved ASC, created_at DESC").fetchall()
        out = []
        for g in goals:
            where_clauses = ["status != 'refunded'"]
            params: list[Any] = []
            if g["platform"] and g["platform"] != "all":
                where_clauses.append("platform = ?")
                params.append(g["platform"])
            if g["event_name"]:
                where_clauses.append("event_name = ?")
                params.append(g["event_name"])
            where_sql = " AND ".join(where_clauses)
            agg = conn.execute(
                f"SELECT COUNT(*) AS cnt, COALESCE(SUM(amount), 0) AS rev FROM sales WHERE {where_sql}",
                params,
            ).fetchone()
            current = float(agg["rev"] if g["metric"] == "revenue" else agg["cnt"])
            target = float(g["target_value"])
            pct = min(round((current / target) * 100, 1), 100.0) if target > 0 else 0.0
            d = dict(g)
            d["achieved"] = 1 if current >= target else 0
            d["current_value"] = round(current, 2)
            d["progress_pct"] = pct
            out.append(d)
    return out
