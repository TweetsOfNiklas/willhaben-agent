"""Redact personal data from a captured willhaben JSON before it becomes a test fixture.

    python tests/redact_fixture.py tests/fixtures/<file>.json [...]

The rules live in app/willhaben/redact.py (shared with `python -m app.willhaben.export`, which turns stored
recordings into fixture files): seller/contact/partner names, e-mail addresses, phone numbers, street addresses,
IBANs, exact coordinates, seller/org/user/conversation ids, every UUID and embedded API keys. Deterministic: the same
input value always maps to the same placeholder, so cross-references inside a fixture stay consistent.
Review the diff afterwards.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.willhaben.redact import redact  # noqa: E402


def main(paths: list[str]) -> None:
    for p in map(Path, paths):
        text, ids = redact(json.loads(p.read_text(encoding="utf-8")))
        p.write_text(text + "\n", encoding="utf-8")
        print(f"{p}: redacted, id map {ids}")


if __name__ == "__main__":
    main(sys.argv[1:])
