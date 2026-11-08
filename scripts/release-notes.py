"""Print the CHANGELOG section for one version, ready to use as GitHub release notes.

  python scripts/release-notes.py 1.0.0
  python scripts/release-notes.py 1.0.0 | gh release create v1.0.0 --title "Manifest 1.0.0" --notes-file -
"""
import re
import sys
from pathlib import Path


def section(changelog: str, version: str) -> str | None:
    m = re.search(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|^\[[^\]]+\]:|\Z)",
                  changelog, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else None


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    text = section((Path(__file__).resolve().parent.parent / "CHANGELOG.md").read_text(encoding="utf-8"), sys.argv[1])
    if not text:
        print(f"No CHANGELOG entry for {sys.argv[1]}", file=sys.stderr)
        return 1
    sys.stdout.reconfigure(encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
