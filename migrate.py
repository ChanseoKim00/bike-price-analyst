"""
DB migration script (runs without psql).
Run: python3 migrate.py

What it does:
  1) Run migrations/init.sql (creates the full schema on a fresh DB; no-op on an existing DB).
  2) Run migrations/NNN_*.sql files in ascending filename order (catch-up for existing DBs).

Every script must be idempotent (IF NOT EXISTS / DROP CONSTRAINT IF EXISTS, etc.).
"""
import glob
import os
import sys
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("[ERROR] DATABASE_URL is not set in the .env file.")
    sys.exit(1)

migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")

# Execution order: init.sql → 001_*.sql → 002_*.sql → ...
scripts = [os.path.join(migrations_dir, "init.sql")]
scripts += sorted(glob.glob(os.path.join(migrations_dir, "[0-9]*.sql")))

try:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    for path in scripts:
        name = os.path.basename(path)
        with open(path, "r") as f:
            sql = f.read()
        cur.execute(sql)
        print(f"[OK] {name}")
    cur.close()
    conn.close()
    print("[OK] migrations complete")
except Exception as e:
    print(f"[ERROR] {e}")
    sys.exit(1)
