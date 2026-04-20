"""
DB 마이그레이션 스크립트 (psql 없이 실행)
실행: python3 migrate.py

동작:
  1) migrations/init.sql 실행 (신규 DB에 전체 스키마 생성, 기존 DB엔 no-op)
  2) migrations/NNN_*.sql 파일을 파일명 오름차순으로 순차 실행 (기존 DB catch-up)

모든 스크립트는 idempotent해야 함 (IF NOT EXISTS / DROP CONSTRAINT IF EXISTS 등).
"""
import glob
import os
import sys
import psycopg2
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.environ.get("DATABASE_URL")
if not DATABASE_URL:
    print("[ERROR] .env 파일에 DATABASE_URL이 없습니다.")
    sys.exit(1)

migrations_dir = os.path.join(os.path.dirname(__file__), "migrations")

# 실행 순서: init.sql → 001_*.sql → 002_*.sql → ...
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
    print("[OK] 마이그레이션 완료")
except Exception as e:
    print(f"[ERROR] {e}")
    sys.exit(1)
