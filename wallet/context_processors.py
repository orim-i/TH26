from django.db import connection
from .models import Goal


def spending_notifications(request):
    if not request.user.is_authenticated:
        return {"spending_alerts": [], "spending_alert_count": 0}

    alerts = []
    goals = list(Goal.objects.filter(user=request.user))
    if not goals:
        # Fallback to seed data used by the goals dashboard
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT id, category, limit_amount, period_start, period_end, current_spend
                FROM wallet_goal
                WHERE user_id = 1
                ORDER BY period_start DESC
                """
            )
            goals = [
                {
                    "category": row[1],
                    "limit_amount": row[2],
                    "period_start": row[3],
                    "period_end": row[4],
                    "current_spend": row[5],
                }
                for row in cur.fetchall()
            ]

    for goal in goals:
        category = goal.category if hasattr(goal, "category") else goal.get("category")
        limit_amount = float(
            goal.limit_amount if hasattr(goal, "limit_amount") else goal.get("limit_amount") or 0
        )
        if limit_amount <= 0:
            continue
        period_start = (
            goal.period_start if hasattr(goal, "period_start") else goal.get("period_start")
        )
        period_end = (
            goal.period_end if hasattr(goal, "period_end") else goal.get("period_end")
        )
        with connection.cursor() as cur:
            cur.execute(
                """
                SELECT COALESCE(SUM(t.amount), 0)
                FROM transactions t
                JOIN transaction_categories c
                  ON c.transaction_id = t.transaction_id
                WHERE c.category LIKE %s
                  AND t.date BETWEEN %s AND %s
                """,
                [f"%{category}%", period_start, period_end],
            )
            tx_spend = float(cur.fetchone()[0] or 0)
        current_spend = float(
            goal.current_spend if hasattr(goal, "current_spend") else goal.get("current_spend") or 0
        )
        effective_spend = max(current_spend, tx_spend)
        percent = (effective_spend / limit_amount) * 100

        if percent >= 100:
            level = "danger"
            threshold = "100%"
        elif percent >= 90:
            level = "warning"
            threshold = "90%"
        elif percent >= 75:
            level = "warning"
            threshold = "75%"
        else:
            continue

        alerts.append(
            {
                "category": category,
                "percent": round(percent),
                "threshold": threshold,
                "level": level,
            }
        )

    alerts.sort(key=lambda a: a["percent"], reverse=True)
    severity = None
    if alerts:
        severity = "danger" if any(a["percent"] >= 100 for a in alerts) else "warning"
    return {
        "spending_alerts": alerts,
        "spending_alert_count": len(alerts),
        "spending_alert_severity": severity,
    }
