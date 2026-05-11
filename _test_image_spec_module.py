"""Verify the behavior of the image_spec_extractor module.

Verification items:
1. should_use_image_mode unit — branch decisions for various ai_result inputs
2. merge_image_specs_into_ai_result unit — confirm it only fills empty slots
3. Real page (cephas) end-to-end — including the Claude API call
"""
import importlib.util
import json
import sys

import requests

# Bypass app/__init__.py
spec = importlib.util.spec_from_file_location(
    "image_spec_extractor", "app/image_spec_extractor.py"
)
ise = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ise)


# ===================== Unit 1: branch decision =====================
print("=" * 70)
print("Unit 1: should_use_image_mode")
print("=" * 70)

EDITOR_HTML = '<html><body>' + \
    "".join(f'<img src="/upload/editor/x{i}.jpg">' for i in range(5)) + \
    '</body></html>'
NO_EDITOR_HTML = '<html><body><img src="/static/logo.png"></body></html>'

cases = [
    (
        "all slots successfully extracted from text (image mode unnecessary)",
        {slot: {"part_name": "x", "part_name_normalized": "x"} for slot in ise.PART_SLOTS},
        EDITOR_HTML, False,
    ),
    (
        "only one slot missing from text (single miss does not trigger image mode)",
        {**{slot: {"part_name": "x"} for slot in ise.PART_SLOTS[:4]},
         "handlebar": {"part_name": None}},
        EDITOR_HTML, False,
    ),
    (
        "two slots missing from text + 5 editor images (image mode branch)",
        {**{slot: {"part_name": "x"} for slot in ise.PART_SLOTS[:3]},
         "saddle": {"part_name": None}, "handlebar": {"part_name": None}},
        EDITOR_HTML, True,
    ),
    (
        "all slots missing from text but 0 editor images (image mode is meaningless)",
        {slot: {"part_name": None} for slot in ise.PART_SLOTS},
        NO_EDITOR_HTML, False,
    ),
    (
        "ai_result is an empty dict (AnalysisError case)",
        {},
        EDITOR_HTML, True,
    ),
]

all_pass = True
for label, ai_result, html, expected in cases:
    use, urls, reason = ise.should_use_image_mode(ai_result, html, "https://x.com/p")
    ok = "PASS" if use == expected else "FAIL"
    if use != expected:
        all_pass = False
    print(f"  {ok} {label}")
    print(f"     decision: {use}, {reason}")
print(f"\n  {'all passed' if all_pass else 'some failures'}")


# ===================== Unit 2: merge =====================
print("\n" + "=" * 70)
print("Unit 2: merge_image_specs_into_ai_result")
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
    "frameset":  {"part_name": "(different value from image)", "part_name_normalized": "different"},  # text takes precedence
    "groupset":  {"part_name": "(different value from image)", "part_name_normalized": "different"},  # text takes precedence
    "wheelset":  {"part_name": "Unaas Hard SE D50", "part_name_normalized": "unaas_hard_se_d50"},
    "saddle":    {"part_name": "Selle Italia Novus", "part_name_normalized": "selle_italia_novus"},
    "handlebar": {"part_name": "Winspace HYPER", "part_name_normalized": "winspace_hyper"},
    "frame_material": "carbon",
    "brake_type": "hydraulic_disc",
    "_meta": {"raw_confidence": {"frame_material": 0.95}},
}
merged = ise.merge_image_specs_into_ai_result(ai_result, image_specs)

checks = [
    ("frameset text result preserved", merged["frameset"]["part_name_normalized"] == "winspace_m6"),
    ("groupset text result preserved", merged["groupset"]["part_name_normalized"] == "shimano_105_di2"),
    ("wheelset filled in from image", merged["wheelset"]["part_name_normalized"] == "unaas_hard_se_d50"),
    ("saddle filled in from image", merged["saddle"]["part_name_normalized"] == "selle_italia_novus"),
    ("handlebar filled in from image", merged["handlebar"]["part_name_normalized"] == "winspace_hyper"),
    ("frame_material unknown -> carbon", merged["frame_material"] == "carbon"),
    ("frame_material_source labeled", merged.get("frame_material_source") == "image_extraction"),
    ("brake_type unknown -> hydraulic_disc", merged["brake_type"] == "hydraulic_disc"),
    ("_image_meta attached", "_image_meta" in merged),
]
unit2_pass = all(ok for _, ok in checks)
for label, ok in checks:
    print(f"  {'PASS' if ok else 'FAIL'} {label}")
print(f"\n  {'all passed' if unit2_pass else 'some failures'}")


# ===================== End-to-end: cephas =====================
print("\n" + "=" * 70)
print("End-to-end: cephas (real Claude API call)")
print("=" * 70)

# Playwright fallback — reuse prototype
sys.modules_to_load = importlib.util.spec_from_file_location(
    "proto", "prototype_image_specs.py"
)
proto = importlib.util.module_from_spec(sys.modules_to_load)
sys.modules_to_load.loader.exec_module(proto)

URL = "https://www.cephas.kr/goods/goods.view.php?gidx=6267"
print(f"  fetching {URL}")
html = proto.fetch_with_playwright(URL)

# Simulate AnalysisError: empty ai_result
empty_ai = {}
use, image_urls, reason = ise.should_use_image_mode(empty_ai, html, URL)
print(f"  branch decision: use={use}, {reason}")
assert use, "cephas: empty ai_result + sufficient editor images -> use_image=True expected"

print(f"  starting image extraction ({len(image_urls)} images)")
result = ise.extract_specs_from_images(image_urls)
print(f"\n  [result]")
for slot in ise.PART_SLOTS:
    s = result[slot]
    mark = "FAIL" if s["part_name"] is None else "OK"
    print(f"    {mark} {slot:10} = {s['part_name_normalized']}")
print(f"    - frame_material = {result['frame_material']}")
print(f"    - brake_type     = {result['brake_type']}")
print(f"\n  [meta]")
m = result["_meta"]
print(f"    - tokens input/output: {m['input_tokens']}/{m['output_tokens']}")
print(f"    - hash: {m['image_url_hash'][:16]}...")
if m["filtered_low_confidence"]:
    print(f"    - cutoff slots: {m['filtered_low_confidence']}")

# Merge simulation: like cephas, assume the text result only contains the model name
fake_text_result = {
    "brand": "refined", "model_name": "REFINED8+ MY26 ULTEGRA Di2 SILENT",
    "groupset": {"part_name": "Shimano Ultegra Di2", "part_name_normalized": "shimano_ultegra_di2"},
    "frameset": None, "wheelset": None, "saddle": None, "handlebar": None,
    "frame_material": "unknown", "brake_type": "unknown",
}
merged = ise.merge_image_specs_into_ai_result(fake_text_result, result)
print(f"\n  [final after merge]")
for slot in ise.PART_SLOTS:
    s = merged.get(slot) or {}
    print(f"    - {slot:10} = {s.get('part_name_normalized')}")

import sys
print("\nAll verifications complete." if (all_pass and unit2_pass) else "\nSome failures.")
sys.exit(0 if (all_pass and unit2_pass) else 1)
