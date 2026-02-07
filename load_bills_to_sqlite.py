import json, sqlite3, sys, os, re, hashlib
from datetime import date, timedelta

def ensure_schema(cur):
    cur.executescript("""
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS accounts (
      account_id     TEXT PRIMARY KEY,
      mask           TEXT,
      name           TEXT,
      official_name  TEXT,
      subtype        TEXT,
      type           TEXT
    );

    CREATE TABLE IF NOT EXISTS transactions (
      transaction_id   TEXT PRIMARY KEY,
      account_id       TEXT NOT NULL,
      amount           REAL NOT NULL,
      date             TEXT NOT NULL,
      name             TEXT,
      merchant_name    TEXT,
      payment_channel  TEXT,
      FOREIGN KEY (account_id) REFERENCES accounts(account_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS transaction_categories (
      transaction_id TEXT NOT NULL,
      idx            INTEGER NOT NULL,
      category       TEXT NOT NULL,
      PRIMARY KEY (transaction_id, idx),
      FOREIGN KEY (transaction_id) REFERENCES transactions(transaction_id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS items (
      item_id         TEXT PRIMARY KEY,
      institution_id  TEXT,
      webhook         TEXT
    );

    CREATE TABLE IF NOT EXISTS meta (
      request_id         TEXT,
      total_transactions INTEGER
    );

    CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id);
    CREATE INDEX IF NOT EXISTS idx_transactions_date   ON transactions(date);
    """)

    # Minimal cards table used by your views (no extra unique constraints)
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='cards'")
    if cur.fetchone() is None:
        cur.executescript("""
        CREATE TABLE cards (
          id                INTEGER PRIMARY KEY AUTOINCREMENT,
          plaid_account_id  TEXT,   -- nullable; we'll link when possible
          card_name         TEXT,
          issuer            TEXT,
          annual_fee        REAL,
          type              TEXT,   -- "credit" / "debit" / etc
          base_reward_rate  REAL
        );
        """)
    else:
        # Add plaid_account_id if missing (older schemas)
        cur.execute("PRAGMA table_info(cards)")
        cols = {row[1] for row in cur.fetchall()}
        if "plaid_account_id" not in cols:
            try:
                cur.execute("ALTER TABLE cards ADD COLUMN plaid_account_id TEXT")
            except sqlite3.OperationalError:
                pass

def _guess_issuer(name: str) -> str:
    if not name: return ""
    n = name.lower()
    issuer_map = {
        "american express": "American Express", "amex": "American Express",
        "chase": "Chase", "jpmorgan": "Chase",
        "bank of america": "Bank of America", "boa": "Bank of America",
        "citi": "Citi", "citibank": "Citi",
        "capital one": "Capital One", "cap one": "Capital One",
        "wells fargo": "Wells Fargo", "discover": "Discover",
        "barclay": "Barclays", "barclays": "Barclays",
        "us bank": "U.S. Bank", "u.s. bank": "U.S. Bank",
    }
    for k, v in issuer_map.items():
        if k in n: return v
    m = re.split(r"\s*[-|–]\s*| card| credit", name, flags=re.I)
    return (m[0] or "").strip()

def _upsert_card_from_account(cur, a):
    """
    Mirror Plaid credit accounts into cards.
    - Update by plaid_account_id if already linked.
    - Else try insert.
    - If insert conflicts on (card_name, issuer) UNIQUE in your DB, just update that row to link plaid_account_id.
    """
    acc_type = (a.get("type") or "").lower()
    if acc_type != "credit":
        return

    plaid_account_id = a.get("account_id")
    card_name = a.get("official_name") or a.get("name") or f"{a.get('type','').title()} {a.get('subtype','')}".strip()
    issuer    = _guess_issuer(a.get("official_name") or a.get("name") or "")
    annual_fee = 0.0
    base_rate  = 1.0
    card_type  = "credit"

    # 1) Update by plaid_account_id
    cur.execute("""
        UPDATE cards
           SET card_name=?, issuer=?, annual_fee=?, type=?, base_reward_rate=?
         WHERE plaid_account_id IS NOT NULL AND plaid_account_id=?
    """, (card_name, issuer, annual_fee, card_type, base_rate, plaid_account_id))
    if cur.rowcount:
        return

    # 2) Try insert (no extra unique constraints here)
    try:
        cur.execute("""
            INSERT INTO cards (plaid_account_id, card_name, issuer, annual_fee, type, base_reward_rate)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (plaid_account_id, card_name, issuer, annual_fee, card_type, base_rate))
    except sqlite3.IntegrityError:
        # 3) If your existing schema enforces UNIQUE(card_name, issuer), link it
        cur.execute("""
            UPDATE cards
               SET plaid_account_id=?, annual_fee=?, type=?, base_reward_rate=?
             WHERE card_name=? AND issuer=?
        """, (plaid_account_id, annual_fee, card_type, base_rate, card_name, issuer))

# seed a single deterministic tx per qualifying account (idempotent)
SEED_RULES = [
    { "match": lambda a: (a.get("type","").lower() == "loan"),
      "name": "Loan Payment (seed)", "merchant": "Loan Servicer",
      "payment_channel": "other", "categories": ["Loan Payment"], "amount": 150.00 },
    { "match": lambda a: (a.get("type","").lower() == "investment" and a.get("subtype","").lower() in {"401k","ira"}),
      "name": "Retirement Contribution (seed)", "merchant": "Plan Provider",
      "payment_channel": "other", "categories": ["Retirement"], "amount": 100.00 },
    { "match": lambda a: (a.get("subtype","").lower() == "hsa"),
      "name": "HSA Contribution (seed)", "merchant": "HSA Custodian",
      "payment_channel": "other", "categories": ["Health","HSA"], "amount": 75.00 },
    { "match": lambda a: (a.get("type","").lower() == "credit"),
      "name": "Credit Card Payment (seed)", "merchant": "Card Issuer",
      "payment_channel": "other", "categories": ["Credit Card","Payment"], "amount": 50.00 },
]

def _seed_date_for_account(acc_id: str, seed_name: str, days_back: int = 14) -> str:
    base = date.today()
    key = f"{acc_id}::{seed_name}".encode("utf-8")
    offset = int(hashlib.md5(key).hexdigest(), 16) % max(1, days_back)
    return (base - timedelta(days=offset)).isoformat()

def _seed_transactions_from_accounts(cur, accounts, seed_on_date=None, days_back: int = 14):
    # If a specific seed_on_date is provided, use it; otherwise spread across past N days.
    fixed_date = seed_on_date

    for a in accounts:
        acc_id = a.get("account_id")
        if not acc_id:
            continue

        for rule in SEED_RULES:
            if rule["match"](a):
                txid = f"seed::{acc_id}::{rule['name']}"
                # Only skip if THIS seed already exists; do NOT skip just because other tx exist
                cur.execute("SELECT 1 FROM transactions WHERE transaction_id=? LIMIT 1", (txid,))
                if cur.fetchone():
                    break

                tx_date = fixed_date or _seed_date_for_account(acc_id, rule["name"], days_back=days_back)
                cur.execute("""
                  INSERT INTO transactions
                    (transaction_id, account_id, amount, date, name, merchant_name, payment_channel)
                  VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (txid, acc_id, float(rule["amount"]), tx_date,
                      rule["name"], rule["merchant"], rule["payment_channel"]))

                # categories for this seed
                for i, cat in enumerate(rule["categories"]):
                    cur.execute("""
                      INSERT OR IGNORE INTO transaction_categories (transaction_id, idx, category)
                      VALUES (?, ?, ?)
                    """, (txid, i, cat))
                break

def load(json_path, db_path):
    if not os.path.exists(json_path):
        raise SystemExit(f"JSON not found: {json_path}")
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    ensure_schema(cur)

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # --- ACCOUNTS (upsert by PK only; account_id) ---
    accounts = data.get("accounts", [])
    for a in accounts:
        cur.execute("""
          INSERT INTO accounts (account_id, mask, name, official_name, subtype, type)
          VALUES (:account_id, :mask, :name, :official_name, :subtype, :type)
          ON CONFLICT(account_id) DO UPDATE SET
            mask=excluded.mask,
            name=excluded.name,
            official_name=excluded.official_name,
            subtype=excluded.subtype,
            type=excluded.type
        """, a)

        # mirror Plaid credit accounts into cards
        _upsert_card_from_account(cur, a)

    # --- REAL TRANSACTIONS from JSON (upsert by transaction_id only) ---
    for t in data.get("transactions", []):
        cur.execute("""
          INSERT INTO transactions (transaction_id, account_id, amount, date, name, merchant_name, payment_channel)
          VALUES (?, ?, ?, ?, ?, ?, ?)
          ON CONFLICT(transaction_id) DO UPDATE SET
            account_id      = excluded.account_id,
            amount          = excluded.amount,
            date            = excluded.date,
            name            = excluded.name,
            merchant_name   = excluded.merchant_name,
            payment_channel = excluded.payment_channel
        """, (
            t.get("transaction_id"),
            t.get("account_id"),
            float(t.get("amount", 0)),
            t.get("date"),
            t.get("name"),
            t.get("merchant_name"),
            t.get("payment_channel"),
        ))

        # categories for this tx (by (transaction_id, idx) only)
        cur.execute("DELETE FROM transaction_categories WHERE transaction_id = ?", (t.get("transaction_id"),))
        for i, cat in enumerate(t.get("category", []) or []):
            cur.execute("""
              INSERT OR IGNORE INTO transaction_categories (transaction_id, idx, category)
              VALUES (?, ?, ?)
            """, (t["transaction_id"], i, cat))

    # --- SEED tx if accounts imply flows; no account-id based skipping ---
    _seed_transactions_from_accounts(cur, accounts)

    # --- ITEM / META (simple writes) ---
    item = data.get("item", {})
    if item and item.get("item_id"):
        cur.execute("""
          INSERT INTO items (item_id, institution_id, webhook)
          VALUES (:item_id, :institution_id, :webhook)
          ON CONFLICT(item_id) DO UPDATE SET
            institution_id = excluded.institution_id,
            webhook        = excluded.webhook
        """, item)

    cur.execute("DELETE FROM meta")
    cur.execute("INSERT INTO meta (request_id, total_transactions) VALUES (?, ?)",
                (data.get("request_id"), data.get("total_transactions")))

    conn.commit()
    conn.close()

if __name__ == "__main__":
    # Usage: python load_bills_to_sqlite.py /path/to/bills.json /path/to/db.sqlite3
    json_path = sys.argv[1] if len(sys.argv) > 1 else "bills.json"
    db_path   = sys.argv[2] if len(sys.argv) > 2 else "db.sqlite3"
    load(json_path, db_path)
    print(f"Loaded {json_path} into {db_path}")
