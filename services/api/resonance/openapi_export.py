"""Write the OpenAPI document used to generate frontend types (owner: lead).

Usage: uv run python -m resonance.openapi_export ../../apps/web/src/api/openapi.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .app import create_app


def main() -> None:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    doc = create_app().openapi()
    text = json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    if target is None:
        sys.stdout.write(text)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
