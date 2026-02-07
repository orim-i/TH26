from django.shortcuts import render, redirect
from datetime import date, timedelta
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.db import connection
from django.db.models import Sum, Count, Q
from django.http import JsonResponse
import json

from .models import Transaction, Card, Deal, Goal, Subscription
import markdown2
from pathlib import Path
from django.conf import settings
from .plaid_pull import sync_plaid_to_sqlite
from importlib.machinery import SourceFileLoader
import sqlite3, os, random
import requests
import certifi
from requests.exceptions import SSLError
from requests.auth import HTTPBasicAuth
import asyncio
from dedalus_labs import AsyncDedalus, DedalusRunner
from django.views.decorators.csrf import csrf_exempt

# configure Dedalus
os.environ["DEDALUS_API_KEY"] = settings.DEDALUS_API_KEY

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


def _visa_pav_verify_pan(pan: str):
    user_id = os.getenv("VISA_PAV_USER_ID")
    password = os.getenv("VISA_PAV_PASSWORD")
    cert_path = os.getenv("VISA_CERT_PATH")
    key_path = os.getenv("VISA_KEY_PATH")
    ca_path = os.getenv("VISA_CA_PATH")
    base_url = os.getenv("VISA_PAV_BASE_URL", "https://sandbox.api.visa.com")

    if not user_id or not password:
        return False, "Visa PAV credentials are not configured."
    if not cert_path or not key_path:
        return False, "Visa client certificate and key are not configured."

    endpoint = f"{base_url}/pav/v1/cardvalidation"

    # Required fields: PAN, acquiring BIN, and country code (plus basic cardAcceptor info)
    stan = f"{random.randint(0, 999999):06d}"
    rrn = f"{random.randint(0, 999999999999):012d}"

    payload = {
        "primaryAccountNumber": pan,
        "acquiringBin": os.getenv("VISA_PAV_ACQUIRING_BIN", "408999"),
        "acquirerCountryCode": os.getenv("VISA_PAV_ACQUIRER_COUNTRY_CODE", "840"),
        "cardAcceptor": {
            "name": "Trove App",
            "terminalId": "TROVE001",
            "idCode": "TROVE001",
            "address": {
                "country": "USA",
                "zipCode": "94404",
                "city": "San Francisco",
                "state": "CA"
            }
        },
        "systemsTraceAuditNumber": stan,
        "retrievalReferenceNumber": rrn,
    }

    verify_opt = ca_path if (ca_path and os.path.exists(ca_path)) else certifi.where()
    try:
        resp = requests.post(
            endpoint,
            json=payload,
            auth=HTTPBasicAuth(user_id, password),
            cert=(cert_path, key_path),
            verify=verify_opt,
            timeout=15,
        )
    except SSLError:
        # Retry with system CA bundle in case custom chain is wrong
        try:
            resp = requests.post(
                endpoint,
                json=payload,
                auth=HTTPBasicAuth(user_id, password),
                cert=(cert_path, key_path),
                verify=certifi.where(),
                timeout=15,
            )
        except Exception as e:
            return False, f"Visa PAV request error: {e}"
    except Exception as e:
        return False, f"Visa PAV request error: {e}"

    if resp.status_code >= 400:
        return False, f"Visa PAV failed ({resp.status_code})."

    data = {}
    try:
        data = resp.json()
    except Exception:
        return False, "Visa PAV returned an invalid response."

    action_code = str(data.get("actionCode", "")).strip()
    if action_code in {"00", "85"}:
        return True, "Verified"
    if action_code:
        return False, f"Action code {action_code}"
    return False, "Verification failed."


@login_required
def dashboard(request):
    transactions = Transaction.objects.order_by("-date")[:5]
    goals = Goal.objects.all()
    cards = Card.objects.all()  # Loads all cards, no user filter
    end_date = date.today()
    start_date = end_date - timedelta(days=6)
    date_keys = [(start_date + timedelta(days=i)) for i in range(7)]
    date_strs = [d.isoformat() for d in date_keys]
    totals_by_date = {d: 0.0 for d in date_strs}
    with connection.cursor() as cur:
        cur.execute(
            """
            SELECT date, COALESCE(SUM(amount), 0)
            FROM transactions
            WHERE date BETWEEN %s AND %s
            GROUP BY date
            """,
            [start_date.isoformat(), end_date.isoformat()],
        )
        for tx_date, total in cur.fetchall():
            totals_by_date[str(tx_date)] = float(total or 0)
    widget_line_categories = [d.strftime("%a") for d in date_keys]
    widget_line_series = [totals_by_date[d] for d in date_strs]

    return render(request, "wallet/dashboard.html", {
        "transactions": transactions,
        "goals": goals,
        "cards": cards,
        "widget_line_categories": json.dumps(widget_line_categories),
        "widget_line_series": json.dumps(widget_line_series),
    })


@login_required
def cards_view(request):
    cards = Card.objects.all()  # Loads all cards, no user filter
    return render(request, "wallet/cards.html", {"cards": cards})

@login_required
def perks_dashboard(request):
    # Revert to raw SQL for cards
    with connection.cursor() as cur:
        cur.execute("""
            SELECT id, name, issuer, annual_fee, card_type, base_reward_rate
            FROM wallet_card
            ORDER BY issuer, name
        """)
        cards = {}
        for row in cur.fetchall():
            card_id, name, issuer, annual_fee, card_type, base_reward_rate = row
            cards[card_id] = {
                "id": card_id,
                "card_name": name,
                "issuer": issuer,
                "annual_fee": float(annual_fee or 0),
                "type": card_type,
                "base_reward_rate": float(base_reward_rate or 0),
                "bonus_categories": [],
                "perks": [],
                "welcome_bonus": None,
                "current_period": None,
            }

    if not cards:
        return render(request, "wallet/deals.html", {"cards": [], "issuers": [], "deals": []})

    valid_ids = set(cards.keys())

    # Keep raw SQL for tables that do NOT have Django Models
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

    # --- Load deals from deals table ---
    with connection.cursor() as cur:
        try:
            cur.execute("""
                SELECT id, card_id, deal_type, title, subtitle, benefit, expiry_date, finer_details, issuer, card_name
                FROM deals
                ORDER BY expiry_date ASC, card_name ASC
            """)
            cols = [c[0] for c in cur.description]
            all_deals = [dict(zip(cols, row)) for row in cur.fetchall()]
        except Exception:
            all_deals = []

    # Filter for only the selected merchants
    selected_merchants = {"Solgaard", "The Bouqs Co.", "Visible by Verizon"}
    deals = [d for d in all_deals if d["title"] in selected_merchants]

    issuers = sorted({(c["issuer"] or "").strip() for c in cards.values() if c["issuer"]})
    return render(request, "wallet/deals.html", {
        "cards": list(cards.values()),
        "issuers": issuers,
        "deals": deals,
    })
    

@login_required
def add_card(request):
    if request.method == "POST":
        card_name = request.POST.get("card_name")
        issuer = request.POST.get("issuer")
        annual_fee = request.POST.get("annual_fee", 0)
        card_type = request.POST.get("type", "credit")
        base_reward_rate = request.POST.get("base_reward_rate", 1)
        pan_raw = request.POST.get("pan", "")
        pan = "".join(ch for ch in pan_raw if ch.isdigit())

        if not pan or len(pan) < 13 or len(pan) > 19:
            messages.error(request, "Please enter a valid card number for verification.")
            return redirect("add_card")

        ok, msg = _visa_pav_verify_pan(pan)
        if not ok:
            messages.error(request, f"Unverified card. {msg}")
            return redirect("add_card")

        try:
            # Used Django ORM instead of raw INSERT
            Card.objects.create(
                user=request.user,
                name=card_name,
                issuer=issuer,
                annual_fee=annual_fee,
                card_type=card_type,
                base_reward_rate=base_reward_rate
            )

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
        # Used Django ORM instead of raw DELETE
        try:
            card = Card.objects.get(id=card_id, user=request.user)
            card.delete()
        except Card.DoesNotExist:
            pass # Handle gracefully or show error
        return redirect('/wallet/cards/')
    
@login_required
def cards_dashboard(request):
    orm_cards = Card.objects.all().order_by("issuer", "name")  # Loads all cards, no user filter

    cards = {}
    for c in orm_cards:
        cards[c.id] = {
            "id": c.id,
            "card_name": c.name,
            "issuer": c.issuer,
            "annual_fee": float(c.annual_fee or 0),
            "type": c.card_type,
            "base_reward_rate": float(c.base_reward_rate or 0),
            "bonus_categories": [],
            "perks": [],
            "welcome_bonus": None,
            "current_period": None,
        }

    valid_ids = set(cards.keys())

    # Keep raw SQL for tables that do NOT have Django Models
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
import asyncio
from dedalus_labs import AsyncDedalus, DedalusRunner
import markdown2
from django.conf import settings
import os

# configure Dedalus
os.environ["DEDALUS_API_KEY"] = settings.DEDALUS_API_KEY


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
            # Use Dedalus to analyze spending
            async def get_analysis():
                client = AsyncDedalus()
                runner = DedalusRunner(client)
                response = await runner.run(
                    input=prompt,
                    model="anthropic/claude-sonnet-4-5-20250929",
                )
                return response.final_output

            resp_text = asyncio.run(get_analysis())
            # convert Markdown -> HTML
            analysis = markdown2.markdown(resp_text)

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

    # Pull card names from the cards table for UI mapping (no DB writes)
    card_names = []
    with connection.cursor() as cur:
        try:
            cur.execute("SELECT card_name FROM cards ORDER BY issuer, card_name")
            card_names = [r[0] for r in cur.fetchall() if r and r[0]]
        except Exception:
            card_names = []

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

    # Subscriptions panel data (read-only, no DB writes)
    subs_qs = Subscription.objects.filter(user=request.user)
    def _manage_url(merchant: str) -> str:
        if not merchant:
            return ""
        m = merchant.lower()
        if "spotify" in m:
            return "https://www.spotify.com/account/overview/"
        if "netflix" in m:
            return "https://www.netflix.com/YourAccount"
        if "amazon" in m:
            return "https://www.amazon.com/gp/help/customer/display.html?nodeId=GTJQ7QZY7QL2HK4Y"
        if "openai" in m or "chatgpt" in m:
            return "https://chatgpt.com/account/manage"
        return ""
    subscriptions = []
    if subs_qs.exists():
        for s in subs_qs:
            subscriptions.append({
                "merchant": s.merchant,
                "amount": float(s.amount),
                "billing_cycle": s.billing_cycle,
                "next_payment_date": s.next_payment_date,
                "last_used_date": None,
                "usage_score": None,
                "prev_amount": None,
                "current_amount": float(s.amount),
                "manage_url": _manage_url(s.merchant),
            })
    else:
        subscriptions = [
            {
                "merchant": "Spotify",
                "amount": 12.99,
                "billing_cycle": "monthly",
                "next_payment_date": date.today() + timedelta(days=6),
                "last_used_date": date.today() - timedelta(days=4),
                "usage_score": 72,
                "prev_amount": 11.99,
                "current_amount": 12.99,
                "manage_url": _manage_url("Spotify"),
            },
            {
                "merchant": "Netflix",
                "amount": 15.49,
                "billing_cycle": "monthly",
                "next_payment_date": date.today() + timedelta(days=18),
                "last_used_date": date.today() - timedelta(days=2),
                "usage_score": 81,
                "prev_amount": None,
                "current_amount": 15.49,
                "manage_url": _manage_url("Netflix"),
            },
            {
                "merchant": "OpenAI",
                "amount": 19.99,
                "billing_cycle": "monthly",
                "next_payment_date": date.today() + timedelta(days=24),
                "last_used_date": date.today() - timedelta(days=15),
                "usage_score": 35,
                "prev_amount": None,
                "current_amount": 19.99,
                "manage_url": _manage_url("OpenAI"),
            },
        ]

    return render(
        request,
        "wallet/goals.html",
        {
            "transactions": transactions,
            "goals": goals,
            "budget": budget,
            "analysis": analysis,
            "card_names": card_names,
            "subscriptions": subscriptions,
        },
    )


@login_required
def subscriptions_dashboard(request):
    subs_qs = Subscription.objects.filter(user=request.user)

    def _manage_url(merchant: str) -> str:
        if not merchant:
            return ""
        m = merchant.lower()
        if "spotify" in m:
            return "https://www.spotify.com/account/overview/"
        if "netflix" in m:
            return "https://www.netflix.com/YourAccount"
        if "amazon" in m:
            return "https://www.amazon.com/gp/help/customer/display.html?nodeId=GTJQ7QZY7QL2HK4Y"
        if "openai" in m or "chatgpt" in m:
            return "https://chatgpt.com/account/manage"
        return ""

    subscriptions = []
    if subs_qs.exists():
        for s in subs_qs:
            subscriptions.append({
                "merchant": s.merchant,
                "amount": float(s.amount),
                "billing_cycle": s.billing_cycle,
                "next_payment_date": s.next_payment_date,
                "last_used_date": None,
                "usage_score": None,
                "prev_amount": None,
                "current_amount": float(s.amount),
                "manage_url": _manage_url(s.merchant),
            })
    else:
        # Sample data for UI preview
        subscriptions = [
            {
                "merchant": "Spotify",
                "amount": 12.99,
                "billing_cycle": "monthly",
                "next_payment_date": date.today() + timedelta(days=6),
                "last_used_date": date.today() - timedelta(days=4),
                "usage_score": 72,
                "prev_amount": 11.99,
                "current_amount": 12.99,
                "manage_url": _manage_url("Spotify"),
            },
            {
                "merchant": "Netflix",
                "amount": 15.49,
                "billing_cycle": "monthly",
                "next_payment_date": date.today() + timedelta(days=18),
                "last_used_date": date.today() - timedelta(days=2),
                "usage_score": 81,
                "prev_amount": None,
                "current_amount": 15.49,
                "manage_url": _manage_url("Netflix"),
            },
            {
                "merchant": "OpenAI",
                "amount": 19.99,
                "billing_cycle": "monthly",
                "next_payment_date": date.today() + timedelta(days=24),
                "last_used_date": date.today() - timedelta(days=15),
                "usage_score": 35,
                "prev_amount": None,
                "current_amount": 19.99,
                "manage_url": _manage_url("OpenAI"),
            },
            {
                "merchant": "Amazon Prime",
                "amount": 14.99,
                "billing_cycle": "monthly",
                "next_payment_date": date.today() + timedelta(days=9),
                "last_used_date": date.today() - timedelta(days=27),
                "usage_score": 22,
                "prev_amount": None,
                "current_amount": 14.99,
                "manage_url": _manage_url("Amazon Prime"),
            },
        ]

    price_alerts = [
        s for s in subscriptions
        if s.get("prev_amount") and s.get("current_amount") and s["current_amount"] > s["prev_amount"]
    ]

    least_used = sorted(
        [s for s in subscriptions if s.get("usage_score") is not None],
        key=lambda x: x["usage_score"]
    )[:3]

    total_monthly = sum(
        s["amount"] for s in subscriptions
        if s.get("billing_cycle") == "monthly"
    )

    next_bill = min(
        (s["next_payment_date"] for s in subscriptions if s.get("next_payment_date")),
        default=None
    )

    return render(
        request,
        "wallet/subscriptions.html",
        {
            "subscriptions": subscriptions,
            "price_alerts": price_alerts,
            "least_used": least_used,
            "total_monthly": total_monthly,
            "next_bill": next_bill,
        },
    )


@login_required
def agent_dashboard(request):
    """
    AI Agent chat interface for financial queries with conversation history
    """
    if request.method == "POST":
        import json
        try:
            data = json.loads(request.body)
            user_message = data.get('message', '').strip()
            conversation_history = data.get('history', [])
            model = data.get('model', 'anthropic/claude-sonnet-4-5-20250929')
            feature = data.get('feature', 'general')

            if not user_message:
                return JsonResponse({'error': 'No message provided'}, status=400)

            # Feature-specific system prompts
            feature_prompts = {
                'general': "You are a helpful financial assistant. Provide clear, actionable advice about spending, budgeting, and financial goals. Keep responses concise and practical.",
                'budget': "You are a budget planning specialist. Help users create, review, and optimize their budget plans. Focus on practical recommendations based on their spending patterns. Provide specific dollar amounts and actionable steps.",
                'analytics': """You are an advanced financial data analyst with expertise in spending pattern analysis and statistical insights.

Your capabilities:
- Analyze transaction data to identify trends, patterns, and anomalies
- Calculate key metrics: averages, percentages, growth rates, and variances
- Identify spending spikes, unusual transactions, and category shifts
- Perform comparative analysis across time periods and categories
- Provide data-driven recommendations with specific numbers

When analyzing data:
1. Start with summary statistics and key findings
2. Calculate relevant percentages and ratios
3. Identify trends over time (increasing/decreasing patterns)
4. Highlight outliers and unusual patterns
5. Compare against typical spending behavior
6. Provide actionable insights based on the numbers

Use tables, bullet points, and clear numerical comparisons. Always show your calculations and reasoning.""",
                'goals': "You are a financial goal tracking expert. Help users track their progress toward financial goals, identify obstacles, and suggest strategies to stay on track. Be encouraging and specific about next steps."
            }

            system_prompt = feature_prompts.get(feature, feature_prompts['general'])

            # Get user's financial context
            with connection.cursor() as cur:
                # Get transaction summary
                cur.execute("""
                    SELECT
                        COUNT(*) as tx_count,
                        COALESCE(SUM(amount), 0) as total_spending,
                        COALESCE(AVG(amount), 0) as avg_amount
                    FROM transactions
                    WHERE date >= date('now', '-30 days')
                """)
                tx_stats = cur.fetchone()

                # Get spending by category
                cur.execute("""
                    SELECT c.category, ROUND(SUM(t.amount), 2) as total
                    FROM transactions t
                    JOIN transaction_categories c ON t.transaction_id = c.transaction_id
                    WHERE t.date >= date('now', '-30 days')
                    GROUP BY c.category
                    ORDER BY total DESC
                    LIMIT 5
                """)
                top_categories = cur.fetchall()

                # Enhanced analytics data (only for analytics feature)
                if feature == 'analytics':
                    # Weekly spending trend (last 4 weeks)
                    cur.execute("""
                        SELECT
                            strftime('%Y-W%W', date) as week,
                            COUNT(*) as tx_count,
                            ROUND(SUM(amount), 2) as total
                        FROM transactions
                        WHERE date >= date('now', '-28 days')
                        GROUP BY week
                        ORDER BY week
                    """)
                    weekly_trend = cur.fetchall()

                    # Top merchants
                    cur.execute("""
                        SELECT
                            merchant,
                            COUNT(*) as tx_count,
                            ROUND(SUM(amount), 2) as total,
                            ROUND(AVG(amount), 2) as avg_amount
                        FROM transactions
                        WHERE date >= date('now', '-30 days')
                        GROUP BY merchant
                        ORDER BY total DESC
                        LIMIT 10
                    """)
                    top_merchants = cur.fetchall()

                    # Spending by category with percentage
                    cur.execute("""
                        SELECT
                            c.category,
                            COUNT(*) as tx_count,
                            ROUND(SUM(t.amount), 2) as total,
                            ROUND(AVG(t.amount), 2) as avg_amount
                        FROM transactions t
                        JOIN transaction_categories c ON t.transaction_id = c.transaction_id
                        WHERE t.date >= date('now', '-30 days')
                        GROUP BY c.category
                        ORDER BY total DESC
                    """)
                    category_breakdown = cur.fetchall()

                    # Comparison with previous period
                    cur.execute("""
                        SELECT
                            COUNT(*) as tx_count,
                            COALESCE(SUM(amount), 0) as total_spending
                        FROM transactions
                        WHERE date >= date('now', '-60 days')
                        AND date < date('now', '-30 days')
                    """)
                    prev_period_stats = cur.fetchone()
                else:
                    weekly_trend = []
                    top_merchants = []
                    category_breakdown = []
                    prev_period_stats = None

            # Get goals from Django ORM
            goals = Goal.objects.filter(user=request.user)
            cards = Card.objects.filter(user=request.user)

            # Build financial context based on feature
            if feature == 'analytics':
                # Enhanced analytics context with detailed data
                financial_context = f"""
FINANCIAL DATA ANALYSIS (Last 30 Days)

=== SUMMARY STATISTICS ===
• Total Transactions: {tx_stats[0]}
• Total Spending: ${tx_stats[1]:.2f}
• Average Transaction: ${tx_stats[2]:.2f}
• Daily Average: ${tx_stats[1]/30:.2f}"""

                # Add previous period comparison
                if prev_period_stats and prev_period_stats[1] > 0:
                    change_pct = ((tx_stats[1] - prev_period_stats[1]) / prev_period_stats[1]) * 100
                    change_indicator = "📈" if change_pct > 0 else "📉"
                    financial_context += f"\n• vs. Previous 30 Days: {change_indicator} {change_pct:+.1f}% (${tx_stats[1] - prev_period_stats[1]:+.2f})"

                # Weekly trend
                if weekly_trend:
                    financial_context += "\n\n=== WEEKLY SPENDING TREND ==="
                    for week, count, total in weekly_trend:
                        financial_context += f"\n• Week {week}: {count} transactions, ${total} total"

                # Category breakdown with percentages
                if category_breakdown:
                    financial_context += "\n\n=== SPENDING BY CATEGORY ==="
                    total_spending = tx_stats[1]
                    for cat, count, total, avg in category_breakdown:
                        pct = (total / total_spending * 100) if total_spending > 0 else 0
                        financial_context += f"\n• {cat}: ${total} ({pct:.1f}%) - {count} transactions @ ${avg} avg"

                # Top merchants
                if top_merchants:
                    financial_context += "\n\n=== TOP MERCHANTS ==="
                    for merchant, count, total, avg in top_merchants[:5]:
                        financial_context += f"\n• {merchant}: ${total} total - {count} transactions @ ${avg} avg"

                if goals.exists():
                    financial_context += "\n\n=== BUDGET GOALS STATUS ==="
                    for goal in goals:
                        pct = (goal.current_spend / goal.limit_amount * 100) if goal.limit_amount > 0 else 0
                        status = "⚠️ OVER BUDGET" if pct > 100 else "✓ On track" if pct < 75 else "⚡ Near limit"
                        remaining = goal.limit_amount - goal.current_spend
                        financial_context += f"\n• {goal.category}: ${goal.current_spend:.2f} / ${goal.limit_amount:.2f} ({pct:.0f}%) - {status} (${remaining:.2f} remaining)"

            else:
                # Standard context for other features
                financial_context = f"""
USER'S FINANCIAL DATA (Last 30 days):
- Transactions: {tx_stats[0]} transactions
- Total Spending: ${tx_stats[1]:.2f}
- Average Transaction: ${tx_stats[2]:.2f}

Top Spending Categories:"""

                for cat, total in top_categories:
                    financial_context += f"\n  • {cat}: ${total}"

                if goals.exists():
                    financial_context += "\n\nACTIVE GOALS:"
                    for goal in goals:
                        pct = (goal.current_spend / goal.limit_amount * 100) if goal.limit_amount > 0 else 0
                        status = "⚠️ Over" if pct > 100 else "✓ On track" if pct < 75 else "⚡ Near limit"
                        financial_context += f"\n  • {goal.category}: ${goal.current_spend:.2f} / ${goal.limit_amount:.2f} ({pct:.0f}%) {status}"

            if cards.exists():
                financial_context += f"\n\nCREDIT CARDS: {cards.count()} cards in wallet"

            # Build message history for Dedalus
            messages = [{"role": "system", "content": system_prompt}]

            # Add conversation history (keep last 10 messages for context)
            for msg in conversation_history[-10:]:
                if msg.get('role') in ['user', 'assistant']:
                    messages.append({
                        "role": msg['role'],
                        "content": msg['content']
                    })

            # Add current message with financial context
            messages.append({
                "role": "user",
                "content": f"{financial_context}\n\nUser Question: {user_message}"
            })

            # Use Dedalus to generate response
            try:
                async def get_ai_response():
                    dedalus = AsyncDedalus()
                    response = await dedalus.chat(
                        messages=messages,
                        model=model
                    )
                    return response

                # Run async function
                response = asyncio.run(get_ai_response())

                return JsonResponse({'response': response})

            except Exception as e:
                return JsonResponse({'error': f'AI service error: {str(e)}'}, status=500)

        except json.JSONDecodeError:
            return JsonResponse({'error': 'Invalid JSON'}, status=400)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=500)

    # GET request - render the chat interface
    return render(request, "wallet/agent.html")


@login_required
def agent_wrapped(request):
    """Return the user's last 30 days of spending as a categorized 'wrapped' summary."""
    # --- auto-sync Plaid Sandbox into SQLite (same as spending_dashboard) ---
    try:
        base = Path(settings.BASE_DIR)
        json_plaid   = (base / "plaid_latest.json").resolve()
        json_bills   = (base / "bills.json").resolve()
        loader_path  = (base / "load_bills_to_sqlite.py").resolve()

        if settings.DATABASES["default"]["ENGINE"].endswith("sqlite3"):
            db_path = Path(settings.DATABASES["default"]["NAME"]).resolve()
        else:
            db_path = (base / "db.sqlite3").resolve()

        sync_plaid_to_sqlite(
            json_plaid_path=json_plaid,
            db_path=db_path,
            loader_path=loader_path,
            bills_json_path=json_bills if json_bills.exists() else None,
            wipe_transactions=True,
        )
    except Exception as e:
        print("Plaid sandbox sync skipped (wrapped):", e)

    try:
        with connection.cursor() as cur:
            # Overall stats
            cur.execute("""
                SELECT
                    COUNT(*) as tx_count,
                    COALESCE(SUM(amount), 0) as total_spending,
                    COALESCE(AVG(amount), 0) as avg_amount,
                    COALESCE(MAX(amount), 0) as max_amount
                FROM transactions
            """)
            row = cur.fetchone()
            tx_count, total_spending, avg_amount, max_amount = row

            # Spending by category
            cur.execute("""
                SELECT c.category, ROUND(SUM(t.amount), 2) as total, COUNT(*) as cnt
                FROM transactions t
                JOIN transaction_categories c ON t.transaction_id = c.transaction_id
                GROUP BY c.category
                ORDER BY total DESC
            """)
            categories = [
                {"name": r[0], "total": float(r[1]), "count": r[2]}
                for r in cur.fetchall()
            ]

            # Top merchant by total spend
            cur.execute("""
                SELECT COALESCE(merchant_name, name, 'Unknown') as merchant,
                       ROUND(SUM(amount), 2) as total, COUNT(*) as cnt
                FROM transactions
                GROUP BY merchant
                ORDER BY total DESC
                LIMIT 1
            """)
            top_merchant_row = cur.fetchone()
            top_merchant = (
                {"name": top_merchant_row[0], "total": float(top_merchant_row[1]), "count": top_merchant_row[2]}
                if top_merchant_row else None
            )

            # Most frequent merchant
            cur.execute("""
                SELECT COALESCE(merchant_name, name, 'Unknown') as merchant,
                       COUNT(*) as cnt, ROUND(SUM(amount), 2) as total
                FROM transactions
                GROUP BY merchant
                ORDER BY cnt DESC
                LIMIT 1
            """)
            freq_merchant_row = cur.fetchone()
            freq_merchant = (
                {"name": freq_merchant_row[0], "count": freq_merchant_row[1], "total": float(freq_merchant_row[2])}
                if freq_merchant_row else None
            )

            # Biggest single purchase
            cur.execute("""
                SELECT COALESCE(merchant_name, name, 'Unknown') as merchant,
                       amount, date
                FROM transactions
                ORDER BY amount DESC
                LIMIT 1
            """)
            biggest_row = cur.fetchone()
            biggest_purchase = (
                {"merchant": biggest_row[0], "amount": float(biggest_row[1]), "date": biggest_row[2]}
                if biggest_row else None
            )

        return JsonResponse({
            "tx_count": tx_count,
            "total_spending": float(total_spending),
            "avg_amount": round(float(avg_amount), 2),
            "categories": categories,
            "top_merchant": top_merchant,
            "freq_merchant": freq_merchant,
            "biggest_purchase": biggest_purchase,
        })

    except Exception as e:
        return JsonResponse({"error": str(e)}, status=500)
