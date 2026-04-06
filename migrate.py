"""
DB 마이그레이션 스크립트 (psql 없이 실행)
실행: python3 migrate.py
"""
import os
import sys
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("[ERROR] .env 파일에 DATABASE_URL이 없습니다.")
    sys.exit(1)

sql_path = os.path.join(os.path.dirname(__file__), "migrations", "init.sql")
with open(sql_path, "r") as f:
    sql = f.read()

try:
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute(sql)
    cur.close()
    conn.close()
    print("[OK] 마이그레이션 완료")
except Exception as e:
    print(f"[ERROR] {e}")
    sys.exit(1)
