import json, sqlite3, sys, os
from typing import Any, Dict, List

def ensure_schema(cur: sqlite3.Cursor):
    cur.executescript("""
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS deals (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      card_id         INTEGER NOT NULL,
      deal_type       TEXT NOT NULL,           -- e.g. 'welcome', 'perk', 'category'
      title           TEXT NOT NULL,           -- e.g. 'Welcome Offer', 'Airport Lounge Access'
      subtitle        TEXT,                    -- e.g. 'After $3000 spend in 3 mo'
      benefit         TEXT,                    -- e.g. '50000 points', '$200 cash back'
      expiry_date     TEXT,                    -- e.g. '2026-09-30'
      finer_details   TEXT,                    -- e.g. 'Activation required', 'Priority Pass Select membership'
      issuer          TEXT,                    -- e.g. 'Chase'
      card_name       TEXT,                    -- e.g. 'Chase Sapphire Reserve'
      FOREIGN KEY (card_id) REFERENCES cards(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS cards (
      id               INTEGER PRIMARY KEY AUTOINCREMENT,
      card_name        TEXT NOT NULL,
      issuer           TEXT,
      annual_fee       REAL,
      type             TEXT,
      base_reward_rate REAL,
      UNIQUE(card_name, issuer)
    );
    """)

def upsert_card(cur: sqlite3.Cursor, c: Dict[str, Any]) -> int:
    card_name = c.get("card_name")
    issuer = c.get("issuer")
    annual_fee = c.get("annual_fee")
    ctype = c.get("type")
    base_rate = c.get("base_reward_rate")

    cur.execute("""
        INSERT INTO cards (card_name, issuer, annual_fee, type, base_reward_rate)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(card_name, issuer) DO UPDATE SET
          annual_fee=excluded.annual_fee,
          type=excluded.type,
          base_reward_rate=excluded.base_reward_rate
    """, (card_name, issuer, annual_fee, ctype, base_rate))

    cur.execute("SELECT id FROM cards WHERE card_name=? AND issuer=?", (card_name, issuer))
    row = cur.fetchone()
    return int(row[0])

def insert_deal(cur: sqlite3.Cursor, deal: Dict[str, Any]):
    cur.execute("""
        INSERT INTO deals (
            card_id, deal_type, title, subtitle, benefit, expiry_date, finer_details, issuer, card_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        deal["card_id"],
        deal["deal_type"],
        deal["title"],
        deal.get("subtitle"),
        deal.get("benefit"),
        deal.get("expiry_date"),
        deal.get("finer_details"),
        deal.get("issuer"),
        deal.get("card_name"),
    ))

def parse_deals(card: Dict[str, Any], card_id: int) -> List[Dict[str, Any]]:
    deals = []
    card_name = card.get("card_name")
    issuer = card.get("issuer")

    # Welcome Bonus
    wb = card.get("welcome_bonus")
    if wb:
        benefit = None
        if wb.get("points"):
            benefit = f"{wb['points']} points"
        elif wb.get("cash_back"):
            benefit = f"${wb['cash_back']} cash back"
        elif wb.get("points_or_cash"):
            benefit = f"${wb['points_or_cash']} value"
        subtitle = ""
        if wb.get("spend_requirement") and wb.get("time_frame_months"):
            subtitle = f"After ${wb['spend_requirement']} in {wb['time_frame_months']} mo"
        elif wb.get("spend_requirement"):
            subtitle = f"After ${wb['spend_requirement']} spend"
        elif wb.get("time_frame_months"):
            subtitle = f"In {wb['time_frame_months']} months"
        expiry_date = wb.get("offer_expiry_date")
        deals.append({
            "card_id": card_id,
            "deal_type": "welcome",
            "title": "Welcome Offer",
            "subtitle": subtitle,
            "benefit": benefit,
            "expiry_date": expiry_date,
            "finer_details": "",
            "issuer": issuer,
            "card_name": card_name,
        })

    # Perks
    for p in card.get("perks", []):
        deals.append({
            "card_id": card_id,
            "deal_type": "perk",
            "title": p.get("perk_name", ""),
            "subtitle": p.get("frequency", ""),
            "benefit": "",
            "expiry_date": None,
            "finer_details": p.get("description", ""),
            "issuer": issuer,
            "card_name": card_name,
        })

    # Bonus Categories
    for bc in card.get("bonus_categories", []):
        title = bc.get("category_name", "")
        if bc.get("reward_rate"):
            title += f" · {bc['reward_rate']}x"
        subtitle = ""
        if bc.get("cap"):
            subtitle = f"Cap ${bc['cap']}"
        if bc.get("note"):
            subtitle += f" · {bc['note']}" if subtitle else bc['note']
        deals.append({
            "card_id": card_id,
            "deal_type": "category",
            "title": title,
            "subtitle": subtitle,
            "benefit": "",
            "expiry_date": None,
            "finer_details": "",
            "issuer": issuer,
            "card_name": card_name,
        })

    return deals

def load(json_path: str, db_path: str):
    if not os.path.exists(json_path):
        raise SystemExit(f"JSON not found: {json_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    ensure_schema(cur)

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    items = data if isinstance(data, list) else [data]

    cur.execute("DELETE FROM deals")  # Clear existing deals

    for card in items:
        card_id = upsert_card(cur, card)
        for deal in parse_deals(card, card_id):
            insert_deal(cur, deal)

    conn.commit()
    conn.close()

if __name__ == "__main__":
    # Usage:
    #   python load_deals_to_sqlite.py /path/to/perk_data.json /path/to/db.sqlite3
    json_path = sys.argv[1] if len(sys.argv) > 1 else "perk_data.json"
    db_path = sys.argv[2] if len(sys.argv) > 2 else "db.sqlite3"
    load(json_path, db_path)
    print(f"Loaded deals from {json_path} into {db_path}")