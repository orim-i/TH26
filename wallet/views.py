from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.db import connection
from django.db.models import Sum, Count, Q

from .models import Transaction, Card, Deal, Goal, Subscription
import markdown2
from pathlib import Path
from django.conf import settings
from .plaid_pull import sync_plaid_to_sqlite
from importlib.machinery import SourceFileLoader
import sqlite3, os
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


@login_required
def dashboard(request):
    transactions = Transaction.objects.order_by("-date")[:5]
    goals = Goal.objects.all()
    cards = Card.objects.all()  # Loads all cards, no user filter
    return render(request, "wallet/dashboard.html", {
        "transactions": transactions,
        "goals": goals,
        "cards": cards,
    })


@login_required
def cards_view(request):
    cards = Card.objects.all()  # Loads all cards, no user filter
    return render(request, "wallet/cards.html", {"cards": cards})

@login_required
def perks_dashboard(request):
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

    if not cards:
        return render(request, "wallet/deals.html", {"cards": [], "issuers": []})

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
        card_type = request.POST.get("type", "credit")
        base_reward_rate = request.POST.get("base_reward_rate", 1)

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
        return redirect('/cards/')
    
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


def get_summary(user_id):
    """
    Refactored to use ORM.
    Requires passing user_id to filter data per user.
    """
    # total spend by category
    # Note: 'transaction_categories' table doesn't have a model, so we rely on Transaction.category
    category_summary_qs = (
        Transaction.objects.filter(user_id=user_id)
        .values("category")
        .annotate(total=Sum("amount"), tx_count=Count("id"))
        .order_by("-total")[:10]
    )

    # overall stats
    overall_stats = Transaction.objects.filter(user_id=user_id).aggregate(
        total_amount=Sum("amount"),
        count=Count("id")
    )
    overall_total = overall_stats['total_amount'] or 0
    tx_count = overall_stats['count'] or 0

    # goals progress
    goals = Goal.objects.filter(user_id=user_id).order_by("-period_start")
    
    summary_text = "Recent spending summary:\n"
    summary_text += f"- Total spent: ${round(overall_total, 2)} across {tx_count} transactions\n\n"

    summary_text += "By category:\n"
    for item in category_summary_qs:
        cat = item['category']
        total = round(item['total'] or 0, 2)
        count = item['tx_count']
        summary_text += f"  • {cat}: ${total} ({count} tx)\n"

    summary_text += "\nGoals progress:\n"
    for g in goals:
        # Calculate spent for this goal using ORM
        spent = Transaction.objects.filter(
            user_id=user_id,
            category__icontains=g.category,
            date__range=(g.period_start, g.period_end)
        ).aggregate(sum=Sum('amount'))['sum'] or 0
        
        summary_text += f"  • {g.category}: ${round(spent, 2)} / ${g.limit_amount}\n"

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
            wipe_transactions=True,
        )
        print("[spending_dashboard] Post-load counts:", counts)
    except Exception as e:
        print("Plaid sandbox sync skipped:", e)


    analysis = None

    # --- Handle POST ---
    if request.method == "POST":
        if "delete_goal_id" in request.POST:
            delete_goal_id = request.POST.get("delete_goal_id")
            # ORM Refactor
            Goal.objects.filter(id=delete_goal_id, user=request.user).delete()

        elif "category" in request.POST:  # add new goal
            category = request.POST.get("category")
            limit_amount = request.POST.get("limit_amount")
            period_start = request.POST.get("period_start")
            period_end = request.POST.get("period_end")

            # ORM Refactor
            Goal.objects.create(
                user=request.user,
                category=category,
                limit_amount=limit_amount,
                current_spend=0,
                period_start=period_start,
                period_end=period_end
            )

        elif "analyze_spending" in request.POST:  # AI button
            summary_text = get_summary(request.user.id)
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
    # ORM Refactor
    transactions = Transaction.objects.order_by("-date")[:100]

    # --- Goals ---
    # ORM Refactor
    db_goals = Goal.objects.order_by("-period_start")

    goals = []
    for g in db_goals:
        # Calculate spend using ORM aggregation matching the category and date range
        current_spend = Transaction.objects.filter(
            user=request.user,
            category__icontains=g.category,
            date__range=(g.period_start, g.period_end)
        ).aggregate(sum=Sum('amount'))['sum'] or 0

        current_spend = float(current_spend)
        limit_amt = float(g.limit_amount)
        
        pct = (current_spend / limit_amt) * 100 if limit_amt else 0
        if pct >= 75:
            color = "#ef4444"
        elif pct >= 50:
            color = "#f59e0b"
        else:
            color = "#22c55e"

        goals.append({
            "id": g.id,
            "category": g.category,
            "limit_amount": g.limit_amount,
            "period_start": g.period_start,
            "period_end": g.period_end,
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


@login_required
def subscriptions_dashboard(request):
    subscriptions = Subscription.objects.all()
    return render(request, "wallet/subscriptions.html", {"subscriptions": subscriptions})