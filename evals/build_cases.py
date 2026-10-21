"""Build (or check) the frozen evaluation case sets.

    cd services/api
    uv run python ../../evals/build_cases.py --check      # verify the committed files
    uv run python ../../evals/build_cases.py --write      # (re)write missing set files

A frozen set is never edited in place: ``--write`` refuses to overwrite an existing file whose
content differs (build a new version, e.g. ``heldout_v2``, after a generator change).
"""

from __future__ import annotations

import argparse
import sys

import casesets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="verify the frozen set files")
    group.add_argument("--write", action="store_true", help="write set files that are missing")
    args = parser.parse_args(argv)
    status = 0
    for name in casesets.SET_NAMES:
        path = casesets.path_for(name)
        content = casesets.dumps(casesets.build(name))
        if path.exists():
            same = path.read_text(encoding="utf-8") == content
            print(f"{name}: {'matches the generator' if same else 'DIFFERS from a fresh build'}")
            if not same:
                status = 1
            continue
        if args.check:
            print(f"{name}: missing ({path})")
            status = 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"{name}: written to {path}")
    if status == 0:
        for name in casesets.SET_NAMES:
            loaded = casesets.load(name)
            print(f"{name}: {len(loaded.cases)} cases, generator {loaded.generator_version}")
    return status


if __name__ == "__main__":
    sys.exit(main())
