from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.db import connection

from .models import Transaction, Card, Deal, Goal, Subscription
import markdown2
from pathlib import Path
from django.conf import settings
from .plaid_pull import sync_plaid_to_sqlite
from importlib.machinery import SourceFileLoader
import sqlite3, os

def sync_plaid_to_sqlite(json_plaid_path, db_path, loader_path, bills_json_path=None, wipe_transactions=True):
    """
    Drops transactions + transaction_categories, then re-creates them by running your loader
    on plaid_latest.json and (optionally) bills.json. Returns simple table counts.
    """
    db_path = str(db_path)
    # 1) Drop the two tables (safe even if they don't exist yet)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    if wipe_transactions:
        cur.executescript("""
            PRAGMA foreign_keys=OFF;
            DROP TABLE IF EXISTS transaction_categories;
            DROP TABLE IF EXISTS transactions;
            PRAGMA foreign_keys=ON;
        """)
    conn.commit()
    conn.close()

    # 2) Import the loader module from its file path and call load(...)
    loader_mod = SourceFileLoader("loader_bills", str(loader_path)).load_module()
    loader_mod.load(str(json_plaid_path), db_path)
    if bills_json_path and os.path.exists(str(bills_json_path)):
        loader_mod.load(str(bills_json_path), db_path)

    # 3) Return quick counts for debugging
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    counts = {}
    for tbl in ("accounts","transactions","transaction_categories","items","meta","cards"):
        try:
            cur.execute(f"SELECT COUNT(*) FROM {tbl}")
            counts[tbl] = cur.fetchone()[0]
        except sqlite3.OperationalError:
            counts[tbl] = 0
    conn.close()
    return counts


@login_required
def dashboard(request):
    transactions = Transaction.objects.filter(user=request.user).order_by("-date")[:5]
    goals = Goal.objects.filter(user=request.user)
    cards = Card.objects.filter(user=request.user)
    return render(request, "wallet/dashboard.html", {
        "transactions": transactions,
        "goals": goals,
        "cards": cards,
    })


@login_required
def cards_view(request):
    cards = Card.objects.filter(user=request.user)
    return render(request, "wallet/cards.html", {"cards": cards})

@login_required
def perks_dashboard(request):
    with connection.cursor() as cur:
        cur.executescript("PRAGMA foreign_keys = ON;")

    cards = {}
    with connection.cursor() as cur:
        cur.execute("""
          SELECT id, card_name, issuer, COALESCE(annual_fee, 0), type, COALESCE(base_reward_rate, 0)
          FROM cards
          ORDER BY issuer, card_name
        """)
        for cid, name, issuer, fee, ctype, base_rate in cur.fetchall():
            cards[cid] = {
                "id": cid,
                "card_name": name or "",
                "issuer": issuer or "",
                "annual_fee": float(fee or 0),
                "type": ctype or "",
                "base_reward_rate": float(base_rate or 0),
                "bonus_categories": [],
                "perks": [],
                "welcome_bonus": None,
                "current_period": None,
            }

    if not cards:
        return render(request, "wallet/deals.html", {"cards": [], "issuers": []})

    valid_ids = set(cards.keys())

    with connection.cursor() as cur:
        cur.execute("""
          SELECT card_id, idx, category_name, reward_rate, cap, note
          FROM bonus_categories
          ORDER BY card_id, idx
        """)
        for card_id, idx, cat_name, rate, cap, note in cur.fetchall():
            if card_id in valid_ids:
                cards[card_id]["bonus_categories"].append({
                    "category_name": cat_name or "",
                    "reward_rate": float(rate or 0),
                    "cap": None if cap is None else float(cap),
                    "note": note or "",
                })

    with connection.cursor() as cur:
        cur.execute("""
          SELECT card_id, idx, perk_name, description, frequency
          FROM perks
          ORDER BY card_id, idx
        """)
        for card_id, idx, perk_name, desc, freq in cur.fetchall():
            if card_id in valid_ids:
                cards[card_id]["perks"].append({
                    "perk_name": perk_name or "",
                    "description": desc or "",
                    "frequency": freq or "",
                })

    with connection.cursor() as cur:
        cur.execute("""
          SELECT card_id, points, cash_back, points_or_cash, spend_requirement, time_frame_months
          FROM welcome_bonuses
        """)
        for card_id, points, cash_back, poc, spend_req, tf_months in cur.fetchall():
            if card_id in valid_ids:
                cards[card_id]["welcome_bonus"] = {
                    "points": None if points is None else int(points),
                    "cash_back": None if cash_back is None else float(cash_back),
                    "points_or_cash": None if poc is None else float(poc),
                    "spend_requirement": None if spend_req is None else float(spend_req),
                    "time_frame_months": None if tf_months is None else int(tf_months),
                }

    with connection.cursor() as cur:
        cur.execute("""
          SELECT card_id, start_date, end_date
          FROM card_current_period
        """)
        for card_id, start_date, end_date in cur.fetchall():
            if card_id in valid_ids:
                cards[card_id]["current_period"] = {
                    "start_date": start_date,
                    "end_date": end_date,
                }

    issuers = sorted({(c["issuer"] or "").strip() for c in cards.values() if c["issuer"]})
    return render(request, "wallet/deals.html", {
        "cards": list(cards.values()),
        "issuers": issuers,
    })
    

@login_required
def add_card(request):
    if request.method == "POST":
        card_name = request.POST.get("card_name")
        issuer = request.POST.get("issuer")
        annual_fee = request.POST.get("annual_fee", 0)
        card_type = request.POST.get("type", "")
        base_reward_rate = request.POST.get("base_reward_rate", 1)

        try:
            with connection.cursor() as cur:
                cur.execute("""
                    INSERT INTO cards (card_name, issuer, annual_fee, type, base_reward_rate)
                    VALUES (%s, %s, %s, %s, %s)
                """, [card_name, issuer, annual_fee, card_type, base_reward_rate])

            messages.success(request, f"✅ {card_name} added successfully!")
            return redirect("cards_dashboard")

        except Exception as e:
            messages.error(request, f"❌ Error adding card: {e}")
            return redirect("cards_dashboard")

    # --- GET request: render the add card form ---
    return render(request, "wallet/add_card.html")


@login_required
def delete_card(request, card_id):
    if request.method == "POST":
        with connection.cursor() as cur:
            cur.execute("DELETE FROM cards WHERE id = %s", [card_id])
        return redirect('/cards/')
    
@login_required
def cards_dashboard(request):
    with connection.cursor() as cur:
        cur.executescript("PRAGMA foreign_keys = ON;")

    cards = {}
    with connection.cursor() as cur:
        cur.execute("""
          SELECT id, card_name, issuer, COALESCE(annual_fee, 0), type, COALESCE(base_reward_rate, 0)
          FROM cards
          ORDER BY issuer, card_name
        """)
        for cid, name, issuer, fee, ctype, base_rate in cur.fetchall():
            cards[cid] = {
                "id": cid,
                "card_name": name or "",
                "issuer": issuer or "",
                "annual_fee": float(fee or 0),
                "type": ctype or "",
                "base_reward_rate": float(base_rate or 0),
                "bonus_categories": [],
                "perks": [],
                "welcome_bonus": None,
                "current_period": None,
            }

    valid_ids = set(cards.keys())

    with connection.cursor() as cur:
        cur.execute("""
          SELECT card_id, idx, category_name, reward_rate, cap, note
          FROM bonus_categories
          ORDER BY card_id, idx
        """)
        for card_id, idx, cat_name, rate, cap, note in cur.fetchall():
            if card_id in valid_ids:
                cards[card_id]["bonus_categories"].append({
                    "category_name": cat_name or "",
                    "reward_rate": float(rate or 0),
                    "cap": None if cap is None else float(cap),
                    "note": note or "",
                })

    with connection.cursor() as cur:
        cur.execute("""
          SELECT card_id, idx, perk_name, description, frequency
          FROM perks
          ORDER BY card_id, idx
        """)
        for card_id, idx, perk_name, desc, freq in cur.fetchall():
            if card_id in valid_ids:
                cards[card_id]["perks"].append({
                    "perk_name": perk_name or "",
                    "description": desc or "",
                    "frequency": freq or "",
                })

    with connection.cursor() as cur:
        cur.execute("""
          SELECT card_id, points, cash_back, points_or_cash, spend_requirement, time_frame_months
          FROM welcome_bonuses
        """)
        for card_id, points, cash_back, poc, spend_req, tf_months in cur.fetchall():
            if card_id in valid_ids:
                cards[card_id]["welcome_bonus"] = {
                    "points": None if points is None else int(points),
                    "cash_back": None if cash_back is None else float(cash_back),
                    "points_or_cash": None if poc is None else float(poc),
                    "spend_requirement": None if spend_req is None else float(spend_req),
                    "time_frame_months": None if tf_months is None else int(tf_months),
                }

    with connection.cursor() as cur:
        cur.execute("""
          SELECT card_id, start_date, end_date
          FROM card_current_period
        """)
        for card_id, start_date, end_date in cur.fetchall():
            if card_id in valid_ids:
                cards[card_id]["current_period"] = {
                    "start_date": start_date,
                    "end_date": end_date,
                }

    # Calculate total annual fee
    total_fee = sum(card["annual_fee"] for card in cards.values())

    return render(request, "wallet/cards.html", {
        "cards": list(cards.values()),
        "total_fee": total_fee
    })


from django.shortcuts import render, redirect
from django.db import connection
from django.views.decorators.csrf import csrf_exempt
import sqlite3
import google.generativeai as genai
import markdown2
from django.conf import settings

# configure Gemini
genai.configure(api_key=settings.GEMINI_API_KEY)
gemini_model = genai.GenerativeModel("gemini-1.5-flash")


def get_summary():
    conn = sqlite3.connect("db.sqlite3")
    cur = conn.cursor()

    # total spend by category (using transaction_categories)
    cur.execute("""
        SELECT c.category, ROUND(SUM(t.amount),2) as total, COUNT(*) as tx_count
        FROM transactions t
        JOIN transaction_categories c
          ON t.transaction_id = c.transaction_id
        GROUP BY c.category
        ORDER BY total DESC
        LIMIT 10;
    """)
    category_summary = cur.fetchall()

    # overall stats
    cur.execute("SELECT ROUND(SUM(amount),2), COUNT(*) FROM transactions;")
    overall_total, tx_count = cur.fetchone()

    # goals progress (compare against categories + date ranges)
    cur.execute("""
        SELECT g.category, g.limit_amount, COALESCE(SUM(t.amount),0) as spent
        FROM wallet_goal g
        LEFT JOIN transactions t
          ON EXISTS (
              SELECT 1 FROM transaction_categories c
              WHERE c.transaction_id = t.transaction_id
              AND c.category LIKE '%' || g.category || '%'
          )
          AND t.date BETWEEN g.period_start AND g.period_end
        GROUP BY g.id
        ORDER BY g.period_start DESC;
    """)
    goals_summary = cur.fetchall()

    conn.close()

    # format summaries as plain text for Gemini
    summary_text = "Recent spending summary:\n"
    summary_text += f"- Total spent: ${overall_total} across {tx_count} transactions\n\n"

    summary_text += "By category:\n"
    for cat, total, count in category_summary:
        summary_text += f"  • {cat}: ${total} ({count} tx)\n"

    summary_text += "\nGoals progress:\n"
    for cat, limit_amt, spent in goals_summary:
        summary_text += f"  • {cat}: ${spent} / ${limit_amt}\n"

    return summary_text

@csrf_exempt
@login_required
def spending_dashboard(request):
     # --- auto-sync Plaid Sandbox into SQLite on each page load ---
    try:
        base = Path(settings.BASE_DIR)
        json_plaid   = (base / "plaid_latest.json").resolve()
        json_bills   = (base / "bills.json").resolve()    # optional
        loader_path  = (base / "load_bills_to_sqlite.py").resolve()

        # Use the SAME DB file Django uses
        if settings.DATABASES["default"]["ENGINE"].endswith("sqlite3"):
            db_path = Path(settings.DATABASES["default"]["NAME"]).resolve()
        else:
            db_path = (base / "db.sqlite3").resolve()

        counts = sync_plaid_to_sqlite(
            json_plaid_path=json_plaid,
            db_path=db_path,
            loader_path=loader_path,
            bills_json_path=json_bills if json_bills.exists() else None,
            wipe_transactions=True,   # <— the destructive refresh you asked for
        )
        print("[spending_dashboard] Post-load counts:", counts)
    except Exception as e:
        print("Plaid sandbox sync skipped:", e)


    analysis = None

    # --- Handle POST ---
    if request.method == "POST":
        if "delete_goal_id" in request.POST:
            delete_goal_id = request.POST.get("delete_goal_id")
            with connection.cursor() as cur:
                cur.execute("DELETE FROM wallet_goal WHERE id = %s AND user_id = 1;", [delete_goal_id])

        elif "category" in request.POST:  # add new goal
            category = request.POST.get("category")
            limit_amount = request.POST.get("limit_amount")
            period_start = request.POST.get("period_start")
            period_end = request.POST.get("period_end")

            with connection.cursor() as cur:
                cur.execute("""
                    INSERT INTO wallet_goal (category, limit_amount, current_spend, period_start, period_end, user_id)
                    VALUES (%s, %s, 0, %s, %s, 1);
                """, [category, limit_amount, period_start, period_end])

        elif "analyze_spending" in request.POST:  # AI button
            summary_text = get_summary()
            prompt = (
                "You are a financial analysis assistant. "
                "Based on this spending summary, identify trends, "
                "check progress on goals, and propose a revised budget plan.\n\n"
                f"{summary_text}"
            )
            resp = gemini_model.generate_content(prompt)
            # convert Markdown -> HTML
            analysis = markdown2.markdown(resp.text)

    # --- Transactions ---
    with connection.cursor() as cur:
        cur.execute("""
            SELECT
                t.transaction_id,
                COALESCE(t.merchant_name, t.name, 'Unknown') AS merchant,
                COALESCE(
                    (SELECT GROUP_CONCAT(c.category, ' / ')
                     FROM transaction_categories c
                     WHERE c.transaction_id = t.transaction_id),
                    ''
                ) AS category,
                t.date AS date,
                t.amount AS amount
            FROM transactions t
            ORDER BY date DESC
            LIMIT 100;
        """)
        cols = [c[0] for c in cur.description]
        rows = cur.fetchall()
        transactions = [dict(zip(cols, r)) for r in rows]

    # --- Goals ---
    with connection.cursor() as cur:
        cur.execute("""
            SELECT id, category, limit_amount, period_start, period_end
            FROM wallet_goal
            WHERE user_id = 1
            ORDER BY period_start DESC;
        """)
        cols = [c[0] for c in cur.description]
        raw_goals = [dict(zip(cols, r)) for r in cur.fetchall()]

    goals = []
    for g in raw_goals:
        with connection.cursor() as cur:
            cur.execute("""
                SELECT COALESCE(SUM(amount), 0)
                FROM transactions t
                JOIN transaction_categories c
                  ON c.transaction_id = t.transaction_id
                WHERE c.category LIKE %s
                  AND t.date BETWEEN %s AND %s;
            """, [f"%{g['category']}%", g["period_start"], g["period_end"]])
            current_spend = float(cur.fetchone()[0] or 0)

        pct = (current_spend / float(g["limit_amount"])) * 100 if g["limit_amount"] else 0
        if pct >= 75:
            color = "#ef4444"
        elif pct >= 50:
            color = "#f59e0b"
        else:
            color = "#22c55e"

        goals.append({
            **g,
            "current_spend": current_spend,
            "pct": pct,
            "color": color,
        })

    budget = sum(float(g["limit_amount"]) for g in goals) if goals else 2000

    return render(
        request,
        "wallet/goals.html",
        {"transactions": transactions, "goals": goals, "budget": budget, "analysis": analysis},
    )
