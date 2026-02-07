from django.shortcuts import render, redirect
from django.contrib.auth import login
from django.contrib.auth.forms import UserCreationForm
from apps.pages.models import Product
from django.core import serializers
from django.contrib.auth.decorators import login_required
import random
from django.shortcuts import redirect
from django.db import connection
from wallet.models import Card, Deal, Goal, Subscription
from django.utils import timezone

#from .models import *

def index(request):
  # daily quotes stuff
  quotes = ["Don't spend more than you earn!", "Save first, spend later.", "Track your expenses daily.", "Invest in your future.", "A penny saved is a penny earned."]
  daily_quote = random.choice(quotes)

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

  all_deals = list(Deal.objects.all())
  deals = random.sample(all_deals, min(2, len(all_deals)))

  issuers = sorted({(c["issuer"] or "").strip() for c in cards.values() if c["issuer"]})

  today = timezone.localdate().isoformat()
  with connection.cursor() as cur:
      cur.execute(
          "SELECT COALESCE(SUM(amount), 0) FROM transactions WHERE date = %s",
          [today],
      )
      daily_spent = float(cur.fetchone()[0] or 0)
  
  # all the deals stuff
  context = {
    'segment': 'dashboard',
    'daily_quote': daily_quote,
    'cards': list(cards.values()),
    'issuers': issuers,
    'deals': deals,
    'daily_spent': daily_spent
  }
  return render(request, "pages/index.html", context)

# Components
def color(request):
  context = {
    'segment': 'color'
  }
  return render(request, "pages/color.html", context)

def typography(request):
  context = {
    'segment': 'typography'
  }
  return render(request, "pages/typography.html", context)

def icon_feather(request):
  context = {
    'segment': 'feather_icon'
  }
  return render(request, "pages/icon-feather.html", context)

def sample_page(request):
  context = {
    'segment': 'sample_page',
  }
  return render(request, 'pages/sample-page.html', context)

def register(request):
    if request.method == 'POST':
        form = UserCreationForm(request.POST)
        if form.is_valid():
            user = form.save()
            login(request, user)  # Log in the user after registration
            return redirect('/dashboard/')  # Redirect to dashboard
    else:
        form = UserCreationForm()
    return render(request, 'accounts/register.html', {'form': form})
