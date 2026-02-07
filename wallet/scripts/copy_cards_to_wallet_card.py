import sqlite3

DB_PATH = "db.sqlite3"

def copy_cards():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # Use COALESCE to set base_reward_rate to 1.0 if NULL
    cur.execute("""
        INSERT INTO wallet_card (name, issuer, annual_fee, card_type, base_reward_rate, user_id)
        SELECT card_name, issuer, annual_fee, type, COALESCE(base_reward_rate, 1.0), 1
        FROM cards
    """)
    conn.commit()
    conn.close()
    print("All cards copied from 'cards' to 'wallet_card' with user_id=1.")

if __name__ == "__main__":
    copy_cards()