import random
from datetime import datetime, timedelta
from typing import Any

from app.database import create_alert, evaluate_goals, get_conn, upsert_sales_batch


def seed_demo_data() -> dict[str, int]:
    """Generate realistic demo sales across Lumepic and Momentia for the last 5 days."""
    rng = random.Random(42)
    now = datetime.now().replace(minute=0, second=0, microsecond=0)

    lumepic_events = [
        ("evt_lume_1", "Maratón Ciudad 2026 - 21K"),
        ("evt_lume_2", "Trail Sierra Alta 42K"),
        ("evt_lume_3", "Gran Fondo Ciclismo Ruta"),
    ]
    momentia_events = [
        ("evt_mom_1", "Maratón Ciudad 2026 - 21K"),
        ("evt_mom_2", "Torneo CrossFit Regional"),
        ("evt_mom_3", "Triatlón Costanera"),
    ]

    buyers = [
        "Lucas M.", "Sofía R.", "Martín G.", "Valentina P.", "Diego L.",
        "Camila F.", "Joaquín S.", "Lucía T.", "Mateo B.", "Paula V.",
    ]

    lumepic_sales: list[dict[str, Any]] = []
    momentia_sales: list[dict[str, Any]] = []

    for hours_ago in range(120, 0, -1):
        ts = now - timedelta(hours=hours_ago)
        hour = ts.hour
        # Peak buying hours: 9-13 and 18-22
        activity_weight = 2.0 if (9 <= hour <= 13 or 18 <= hour <= 22) else 0.6

        lume_count = rng.choices([0, 1, 2, 3], weights=[3, 4 * activity_weight, 2 * activity_weight, activity_weight])[0]
        for i in range(lume_count):
            ev_id, ev_name = rng.choice(lumepic_events)
            items = rng.choices([1, 2, 3, 5], weights=[5, 3, 2, 1])[0]
            price_per_photo = rng.choice([2500, 3000, 3500])
            sold_dt = ts + timedelta(minutes=rng.randint(0, 59))
            lumepic_sales.append(
                {
                    "external_id": f"demo_lume_{hours_ago}_{i}",
                    "event_external_id": ev_id,
                    "event_name": ev_name,
                    "sold_at": sold_dt.isoformat(timespec="seconds"),
                    "amount": float(items * price_per_photo),
                    "items_count": items,
                    "buyer_name": rng.choice(buyers),
                    "status": "completed",
                    "raw": {"demo": True},
                }
            )

        mom_count = rng.choices([0, 1, 2], weights=[4, 3 * activity_weight, 1.5 * activity_weight])[0]
        for i in range(mom_count):
            ev_id, ev_name = rng.choice(momentia_events)
            items = rng.choices([1, 2, 4], weights=[5, 3, 2])[0]
            price_per_photo = rng.choice([2800, 3200, 4000])
            sold_dt = ts + timedelta(minutes=rng.randint(0, 59))
            momentia_sales.append(
                {
                    "external_id": f"demo_mom_{hours_ago}_{i}",
                    "event_external_id": ev_id,
                    "event_name": ev_name,
                    "sold_at": sold_dt.isoformat(timespec="seconds"),
                    "amount": float(items * price_per_photo),
                    "items_count": items,
                    "buyer_name": rng.choice(buyers),
                    "status": "completed",
                    "raw": {"demo": True},
                }
            )

    res_l = upsert_sales_batch("lumepic", lumepic_sales, is_demo=1)
    res_m = upsert_sales_batch("momentia", momentia_sales, is_demo=1)

    with get_conn() as conn:
        existing_goals = conn.execute("SELECT COUNT(*) AS c FROM goals").fetchone()["c"]
        if existing_goals == 0:
            conn.execute(
                "INSERT INTO goals (name, platform, metric, target_value) VALUES (?, ?, ?, ?)",
                ("Meta Semanal Unificada ($500.000)", "all", "revenue", 500000),
            )
            conn.execute(
                "INSERT INTO goals (name, platform, metric, target_value) VALUES (?, ?, ?, ?)",
                ("100 Ventas en Lumepic", "lumepic", "count", 100),
            )

    evaluate_goals()
    create_alert(
        "info",
        "[DEMO] Datos de demostración cargados",
        f"Se generaron {res_l['new_count']} ventas en Lumepic y {res_m['new_count']} en Momentia para probar el tablero.",
        platform="all",
    )
    return {"lumepic": res_l["new_count"], "momentia": res_m["new_count"]}
