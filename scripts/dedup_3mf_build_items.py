#!/usr/bin/env python3
"""Remove duplicate <item> entries from a Bambu 3MF's <build> section.

Two items are considered duplicates when they reference the same logical part
(via objectid -> resource component path -> model_settings name) and have
identical transform attributes. Keeps the first occurrence; drops the rest.

Resources, per-part object_*.model files, and model_settings.config entries are
left untouched (orphans are inert; Bambu will not render or print them).
"""
import argparse
import re
import shutil
import sys
import zipfile
from collections import defaultdict
from pathlib import Path

ITEM_RE = re.compile(
    r'<item\s+objectid="(\d+)"(?:\s+p:UUID="[^"]*")?\s+transform="([^"]+)"[^/]*/>'
)


def parse_resource_to_path(model_xml: str) -> dict[str, str]:
    """resource object id -> per-part component path."""
    out = {}
    for m in re.finditer(
        r'<object id="(\d+)"[^>]*>\s*<components>\s*<component p:path="([^"]+)"',
        model_xml,
    ):
        out[m.group(1)] = m.group(2).lstrip("/")
    return out


def parse_resource_to_name(cfg_xml: str) -> dict[str, str]:
    """model_settings object id -> part name."""
    out = {}
    for m in re.finditer(
        r'<object\s+id="(\d+)">\s*<metadata key="name" value="([^"]+)"/>',
        cfg_xml,
    ):
        out[m.group(1)] = m.group(2)
    return out


def dedup(model_xml: str, name_by_resid: dict[str, str]) -> tuple[str, int, int]:
    """Return new model_xml, kept_count, dropped_count."""
    seen: set[tuple[str, str]] = set()
    kept = dropped = 0

    def repl(m: re.Match) -> str:
        nonlocal kept, dropped
        objectid, transform = m.group(1), m.group(2)
        name = name_by_resid.get(objectid, f"?{objectid}")
        key = (name, transform)
        if key in seen:
            dropped += 1
            return ""
        seen.add(key)
        kept += 1
        return m.group(0)

    new_xml = ITEM_RE.sub(repl, model_xml)
    # Collapse the blank lines we just left behind
    new_xml = re.sub(r"(\r?\n)[ \t]*(?=\r?\n)", r"\1", new_xml)
    return new_xml, kept, dropped


def rewrite_zip(src: Path, dst: Path, new_model_xml: str) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
        dst, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for info in zin.infolist():
            data = (
                new_model_xml.encode("utf-8")
                if info.filename == "3D/3dmodel.model"
                else zin.read(info.filename)
            )
            # preserve compression behavior per-entry
            zout.writestr(info, data)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with zipfile.ZipFile(args.path) as z:
        model_xml = z.read("3D/3dmodel.model").decode("utf-8")
        cfg_xml = z.read("Metadata/model_settings.config").decode("utf-8")

    res_to_path = parse_resource_to_path(model_xml)
    name_by_resid = parse_resource_to_name(cfg_xml)

    # Diagnostic: per-part stacked count
    items = ITEM_RE.findall(model_xml)
    by_name: dict[str, list[str]] = defaultdict(list)
    for oid, tr in items:
        by_name[name_by_resid.get(oid, f"?{oid}")].append(tr)

    print(f"Total <item> entries: {len(items)}")
    print(f"Distinct parts: {len(by_name)}")
    stacked = []
    for name, trs in by_name.items():
        if len(trs) != len(set(trs)):
            stacked.append((name, len(trs), len(set(trs))))
    stacked.sort(key=lambda x: -(x[1] - x[2]))
    for name, total, uniq in stacked:
        print(f"  {name:40s} total={total:3d} unique_xforms={uniq:3d} drops={total - uniq}")

    new_xml, kept, dropped = dedup(model_xml, name_by_resid)
    print(f"\nKept: {kept} items   Dropped: {dropped} items")

    if args.dry_run:
        print("(dry run — no changes written)")
        return 0

    tmp = args.path.with_suffix(args.path.suffix + ".dedup.tmp")
    rewrite_zip(args.path, tmp, new_xml)
    shutil.move(tmp, args.path)
    print(f"Wrote {args.path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
