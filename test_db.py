"""
DB 연결 및 스키마 검증 스크립트
실행: python test_db.py
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

EXPECTED_TABLES = {
    "parts": [
        "id", "part_type", "part_name", "part_name_normalized",
        "price_krw", "official_url", "last_verified_at", "last_checked_at",
        "ttl_days", "created_at",
    ],
    "bikes": [
        "id", "brand", "model_name", "model_year", "price_krw", "official_url",
        "frame_material", "frame_material_confidence", "frame_material_source",
        "brake_type", "groupset_id", "wheelset_id", "saddle_id",
        "weight_kg", "last_verified_at", "stale", "created_at",
    ],
    "analyses": [
        "id", "bike_id", "parts_sum_krw", "saving_krw",
        "saving_pct", "missing_parts", "analyzed_at",
    ],
}

EXPECTED_INDEXES = [
    "idx_parts_last_checked_at",
    "idx_parts_part_name_normalized",
]


def check(label, ok, detail=""):
    status = "OK" if ok else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail else ""))
    return ok


def main():
    print("=" * 55)
    print("  Bike Price Analyst — DB 연결 및 스키마 검증")
    print("=" * 55)

    # 1. 연결
    print("\n[1] DB 연결")
    try:
        conn = psycopg2.connect(DATABASE_URL)
        cur = conn.cursor()
        check("연결 성공", True, DATABASE_URL.split("@")[-1])
    except Exception as e:
        check("연결 실패", False, str(e))
        sys.exit(1)

    all_ok = True

    # 2. 테이블 + 컬럼 확인
    print("\n[2] 테이블 및 컬럼 확인")
    cur.execute("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
        ORDER BY table_name, ordinal_position
    """)
    rows = cur.fetchall()
    actual = {}
    for table, col in rows:
        actual.setdefault(table, []).append(col)

    for table, expected_cols in EXPECTED_TABLES.items():
        exists = table in actual
        ok = check(f"테이블 '{table}' 존재", exists)
        all_ok = all_ok and ok
        if exists:
            missing = [c for c in expected_cols if c not in actual[table]]
            ok2 = check(
                f"  컬럼 전체 확인",
                not missing,
                f"누락: {missing}" if missing else f"{len(actual[table])}개 컬럼 OK",
            )
            all_ok = all_ok and ok2

    # 3. 인덱스 확인
    print("\n[3] 인덱스 확인")
    cur.execute("""
        SELECT indexname
        FROM pg_indexes
        WHERE schemaname = 'public'
    """)
    actual_indexes = {r[0] for r in cur.fetchall()}
    for idx in EXPECTED_INDEXES:
        ok = check(f"인덱스 '{idx}'", idx in actual_indexes)
        all_ok = all_ok and ok

    # 4. UNIQUE 제약 확인
    print("\n[4] UNIQUE 제약 확인")
    cur.execute("""
        SELECT conname
        FROM pg_constraint
        WHERE contype = 'u' AND conrelid = 'bikes'::regclass
    """)
    unique_constraints = {r[0] for r in cur.fetchall()}
    ok = check("bikes (brand, model_name, model_year) UNIQUE", bool(unique_constraints),
               str(unique_constraints) if unique_constraints else "없음")
    all_ok = all_ok and ok

    # 5. FK 확인
    print("\n[5] FK 제약 확인")
    cur.execute("""
        SELECT conname, conrelid::regclass AS "table"
        FROM pg_constraint
        WHERE contype = 'f'
          AND conrelid IN ('bikes'::regclass, 'analyses'::regclass)
        ORDER BY conrelid::regclass::text, conname
    """)
    fk_rows = cur.fetchall()
    fk_names = {r[0] for r in fk_rows}
    ok = check("FK 존재 (bikes → parts, analyses → bikes)",
               len(fk_rows) >= 4, f"발견된 FK: {fk_names}")
    all_ok = all_ok and ok

    cur.close()
    conn.close()

    print("\n" + "=" * 55)
    if all_ok:
        print("  결과: 모든 검증 통과")
    else:
        print("  결과: 일부 검증 실패 — 위 FAIL 항목 확인")
    print("=" * 55)
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
