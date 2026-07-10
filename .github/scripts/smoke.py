#!/usr/bin/env python3
"""Cross-platform import + pure-function smoke test. No network, no deps."""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))  # repo root
import banto  # noqa: E402

assert banto.VERSION, "VERSION missing"

# hardware shape detection runs on the CI runner (best-effort, no GPU expected)
s = banto.shape()
assert "shape_hash" in s and "usable_gb" in s, f"bad shape: {s}"

# roofline fit is a pure calculation
f = banto.fit({"params_b": 120, "active_params_b": 12, "quant_bits": 4, "context": 32768})
assert "verdict" in f and "fits" in f, f"bad fit: {f}"

# self-update surface exists and its pure parts behave (no network here)
assert callable(banto.check_update) and callable(banto.self_update)
assert banto._ver_tuple("v1.2.3") == (1, 2, 3)
assert banto._ver_tuple("0.5.0") > banto._ver_tuple("0.4.9")
assert "banto.py" in banto._asset_preference(), "portable .py fallback must always be offered"

print(f"smoke ok: banto {banto.VERSION} shape={s['shape_hash']} fit={f['verdict']}")
