-- 003_parts_snapshot.sql
-- /result/<analysis_id> 렌더 시 분석 당시 부품 구성을 재현하기 위해
-- analyses 테이블에 부품 ID 스냅샷(JSONB)을 추가.
--
-- 형식: {"groupset": "<uuid>", "wheelset": "<uuid>|null",
--        "frameset": "<uuid>", "saddle": "<uuid>|null",
--        "handlebar": "<uuid>|null"}
--
-- 기존 row는 NULL로 남고, /result 렌더에서 NULL인 경우 bike FK 기반 폴백 로직을 쓴다.

ALTER TABLE analyses
    ADD COLUMN IF NOT EXISTS parts_snapshot JSONB;
