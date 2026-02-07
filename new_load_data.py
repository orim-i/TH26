import json, sqlite3, sys, os

def ensure_schema(cur):
    cur.executescript("""
    PRAGMA foreign_keys = ON;
    CREATE TABLE IF NOT EXISTS deals (
      id              INTEGER PRIMARY KEY AUTOINCREMENT,
      card_id         INTEGER NOT NULL,
      deal_type       TEXT NOT NULL,
      title           TEXT NOT NULL,
      subtitle        TEXT,
      benefit         TEXT,
      expiry_date     TEXT,
      finer_details   TEXT,
      issuer          TEXT,
      card_name       TEXT,
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

def get_card_id(cur, card_name, issuer):
    cur.execute("SELECT id FROM cards WHERE card_name=? AND issuer=?", (card_name, issuer))
    row = cur.fetchone()
    if row:
        return int(row[0])
    # Insert if not found
    cur.execute("""
        INSERT INTO cards (card_name, issuer)
        VALUES (?, ?)
        ON CONFLICT(card_name, issuer) DO NOTHING
    """, (card_name, issuer))
    cur.execute("SELECT id FROM cards WHERE card_name=? AND issuer=?", (card_name, issuer))
    return int(cur.fetchone()[0])

def insert_deal(cur, deal, card_id, card_name, issuer):
    cur.execute("""
        INSERT INTO deals (
            card_id, deal_type, title, subtitle, benefit, expiry_date, finer_details, issuer, card_name
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        card_id,
        deal["deal_type"],
        deal["merchant"],
        None,
        deal["offer"],
        deal["expiry_date"],
        f"{deal['reward_rate']}% back" if deal["reward_rate"] else "",
        issuer,
        card_name,
    ))

def load(json_path, db_path):
    if not os.path.exists(json_path):
        raise SystemExit(f"JSON not found: {json_path}")

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    ensure_schema(cur)

    with open(json_path, "r", encoding="utf-8") as f:
        deals = json.load(f)

    # All deals are for Chase Freedom Flex
    card_name = "Chase Freedom Flex"
    issuer = "Chase"
    card_id = get_card_id(cur, card_name, issuer)

    for deal in deals:
        insert_deal(cur, deal, card_id, card_name, issuer)

    conn.commit()
    conn.close()

if __name__ == "__main__":
    # Usage: python load_chase_deals_to_sqlite.py deals_data.json db.sqlite3
    json_path = sys.argv[1] if len(sys.argv) > 1 else "deals_data.json"
    db_path = sys.argv[2] if len(sys.argv) > 2 else "db.sqlite3"
    load(json_path, db_path)
    print(f"Loaded deals from {json_path} into {db_path}")