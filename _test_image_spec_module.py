"""image_spec_extractor 모듈 동작 검증.

검증 항목:
1. should_use_image_mode 단위 — 다양한 ai_result 입력에 대한 분기 판정
2. merge_image_specs_into_ai_result 단위 — 빈 슬롯만 채우는지
3. 실 페이지(cephas) end-to-end — Claude API 호출까지
"""
import importlib.util
import json
import sys

import requests

# app/__init__.py 우회
spec = importlib.util.spec_from_file_location(
    "image_spec_extractor", "app/image_spec_extractor.py"
)
ise = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ise)


# ===================== 단위 1: 분기 판정 =====================
print("=" * 70)
print("단위 1: should_use_image_mode")
print("=" * 70)

EDITOR_HTML = '<html><body>' + \
    "".join(f'<img src="/upload/editor/x{i}.jpg">' for i in range(5)) + \
    '</body></html>'
NO_EDITOR_HTML = '<html><body><img src="/static/logo.png"></body></html>'

cases = [
    (
        "텍스트로 모든 슬롯 추출 성공 (이미지 모드 불필요)",
        {slot: {"part_name": "x", "part_name_normalized": "x"} for slot in ise.PART_SLOTS},
        EDITOR_HTML, False,
    ),
    (
        "텍스트로 1개만 누락 (단일 누락은 이미지 모드 안 씀)",
        {**{slot: {"part_name": "x"} for slot in ise.PART_SLOTS[:4]},
         "handlebar": {"part_name": None}},
        EDITOR_HTML, False,
    ),
    (
        "텍스트로 2개 누락 + editor 이미지 5장 (이미지 모드 분기)",
        {**{slot: {"part_name": "x"} for slot in ise.PART_SLOTS[:3]},
         "saddle": {"part_name": None}, "handlebar": {"part_name": None}},
        EDITOR_HTML, True,
    ),
    (
        "텍스트로 모두 누락이지만 editor 이미지 0장 (이미지 모드 의미 없음)",
        {slot: {"part_name": None} for slot in ise.PART_SLOTS},
        NO_EDITOR_HTML, False,
    ),
    (
        "ai_result가 빈 dict (AnalysisError 케이스)",
        {},
        EDITOR_HTML, True,
    ),
]

all_pass = True
for label, ai_result, html, expected in cases:
    use, urls, reason = ise.should_use_image_mode(ai_result, html, "https://x.com/p")
    ok = "✓" if use == expected else "✗"
    if use != expected:
        all_pass = False
    print(f"  {ok} {label}")
    print(f"     판정: {use}, {reason}")
print(f"\n  {'전체 통과' if all_pass else '실패 있음'}")


# ===================== 단위 2: 병합 =====================
print("\n" + "=" * 70)
print("단위 2: merge_image_specs_into_ai_result")
print("=" * 70)

ai_result = {
    "brand": "winspace", "model_name": "M6",
    "frameset": {"part_name": "Winspace M6", "part_name_normalized": "winspace_m6"},
    "groupset": {"part_name": "Shimano 105 Di2", "part_name_normalized": "shimano_105_di2"},
    "wheelset": {"part_name": None, "part_name_normalized": None},
    "saddle":   {"part_name": None, "part_name_normalized": None},
    "handlebar": {"part_name": None, "part_name_normalized": None},
    "frame_material": "unknown",
    "brake_type": "unknown",
}
image_specs = {
    "frameset":  {"part_name": "(이미지에서 다른 값)", "part_name_normalized": "different"},  # 텍스트 우선
    "groupset":  {"part_name": "(이미지에서 다른 값)", "part_name_normalized": "different"},  # 텍스트 우선
    "wheelset":  {"part_name": "Unaas Hard SE D50", "part_name_normalized": "unaas_hard_se_d50"},
    "saddle":    {"part_name": "Selle Italia Novus", "part_name_normalized": "selle_italia_novus"},
    "handlebar": {"part_name": "Winspace HYPER", "part_name_normalized": "winspace_hyper"},
    "frame_material": "carbon",
    "brake_type": "hydraulic_disc",
    "_meta": {"raw_confidence": {"frame_material": 0.95}},
}
merged = ise.merge_image_specs_into_ai_result(ai_result, image_specs)

checks = [
    ("frameset 텍스트 결과 유지", merged["frameset"]["part_name_normalized"] == "winspace_m6"),
    ("groupset 텍스트 결과 유지", merged["groupset"]["part_name_normalized"] == "shimano_105_di2"),
    ("wheelset 이미지로 보강", merged["wheelset"]["part_name_normalized"] == "unaas_hard_se_d50"),
    ("saddle 이미지로 보강", merged["saddle"]["part_name_normalized"] == "selle_italia_novus"),
    ("handlebar 이미지로 보강", merged["handlebar"]["part_name_normalized"] == "winspace_hyper"),
    ("frame_material unknown→carbon", merged["frame_material"] == "carbon"),
    ("frame_material_source 표시", merged.get("frame_material_source") == "image_extraction"),
    ("brake_type unknown→hydraulic_disc", merged["brake_type"] == "hydraulic_disc"),
    ("_image_meta 첨부", "_image_meta" in merged),
]
unit2_pass = all(ok for _, ok in checks)
for label, ok in checks:
    print(f"  {'✓' if ok else '✗'} {label}")
print(f"\n  {'전체 통과' if unit2_pass else '실패 있음'}")


# ===================== End-to-end: cephas =====================
print("\n" + "=" * 70)
print("End-to-end: cephas (실제 Claude API 호출)")
print("=" * 70)

# Playwright 폴백 — prototype 재사용
sys.modules_to_load = importlib.util.spec_from_file_location(
    "proto", "prototype_image_specs.py"
)
proto = importlib.util.module_from_spec(sys.modules_to_load)
sys.modules_to_load.loader.exec_module(proto)

URL = "https://www.cephas.kr/goods/goods.view.php?gidx=6267"
print(f"  fetching {URL}")
html = proto.fetch_with_playwright(URL)

# AnalysisError 발생 시뮬: 빈 ai_result
empty_ai = {}
use, image_urls, reason = ise.should_use_image_mode(empty_ai, html, URL)
print(f"  분기 판정: use={use}, {reason}")
assert use, "cephas는 빈 ai_result + editor 이미지 충분 → use_image=True여야 함"

print(f"  이미지 추출 시작 ({len(image_urls)}장)")
result = ise.extract_specs_from_images(image_urls)
print(f"\n  [결과]")
for slot in ise.PART_SLOTS:
    s = result[slot]
    mark = "✗" if s["part_name"] is None else "✓"
    print(f"    {mark} {slot:10} = {s['part_name_normalized']}")
print(f"    · frame_material = {result['frame_material']}")
print(f"    · brake_type     = {result['brake_type']}")
print(f"\n  [메타]")
m = result["_meta"]
print(f"    · 토큰 input/output: {m['input_tokens']}/{m['output_tokens']}")
print(f"    · 해시: {m['image_url_hash'][:16]}...")
if m["filtered_low_confidence"]:
    print(f"    · 컷오프된 슬롯: {m['filtered_low_confidence']}")

# 병합 시뮬: cephas 같이 텍스트엔 모델명만 있는 케이스를 가정
fake_text_result = {
    "brand": "refined", "model_name": "REFINED8+ MY26 ULTEGRA Di2 SILENT",
    "groupset": {"part_name": "Shimano Ultegra Di2", "part_name_normalized": "shimano_ultegra_di2"},
    "frameset": None, "wheelset": None, "saddle": None, "handlebar": None,
    "frame_material": "unknown", "brake_type": "unknown",
}
merged = ise.merge_image_specs_into_ai_result(fake_text_result, result)
print(f"\n  [병합 후 최종]")
for slot in ise.PART_SLOTS:
    s = merged.get(slot) or {}
    print(f"    · {slot:10} = {s.get('part_name_normalized')}")

import sys
print("\n전체 검증 완료." if (all_pass and unit2_pass) else "\n실패 있음.")
sys.exit(0 if (all_pass and unit2_pass) else 1)
