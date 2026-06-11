#!/usr/bin/env python3
"""Remove duplicate stacked instances from a Bambu 3MF.

For each (part_name, build_transform) group with multiple instances, keeps the
first occurrence and drops the rest. Each drop cascades through the four sites
that reference the resource id:

  3D/3dmodel.model:
    - <item objectid="X" .../> in <build>
    - <object id="X" type="model">...</object> in <resources>
  Metadata/model_settings.config:
    - <object id="X">...</object> wrapper block (includes inner <part>)
    - <model_instance> blocks with <metadata key="object_id" value="X"/>
    - <assemble_item object_id="X" .../>

Per-part 3D/Objects/object_N.model files are left in place (multiple resources
can share the same per-part file; safe-side: keep them).
"""
import argparse
import re
import shutil
import sys
import xml.etree.ElementTree as ET
import zipfile
from collections import defaultdict
from pathlib import Path

ITEM_RE = re.compile(
    r'<item\s+objectid="(\d+)"(?:\s+p:UUID="[^"]*")?\s+transform="([^"]+)"[^/]*/>'
)


def parse_name_by_resid(cfg_xml: str) -> dict[str, str]:
    out = {}
    for m in re.finditer(
        r'<object\s+id="(\d+)">\s*<metadata key="name" value="([^"]+)"/>',
        cfg_xml,
    ):
        out[m.group(1)] = m.group(2)
    return out


def pick_drops(model_xml: str, name_by_resid: dict[str, str]) -> list[str]:
    """Return resource IDs to drop, keeping first per (name, transform) group."""
    seen: set[tuple[str, str]] = set()
    drops: list[str] = []
    for m in ITEM_RE.finditer(model_xml):
        oid, tr = m.group(1), m.group(2)
        name = name_by_resid.get(oid, f"?{oid}")
        key = (name, tr)
        if key in seen:
            drops.append(oid)
        else:
            seen.add(key)
    return drops


def remove_build_items(model_xml: str, drops: set[str]) -> str:
    def repl(m: re.Match) -> str:
        return "" if m.group(1) in drops else m.group(0)
    new = ITEM_RE.sub(repl, model_xml)
    return collapse_blank_lines(new)


def remove_resource_objects(model_xml: str, drops: set[str]) -> str:
    """Remove <object id="X" ...>...</object> blocks from <resources>."""
    # Resource object blocks are non-nested at this level; <component .../> is self-closing
    pattern = re.compile(
        r'\s*<object id="(\d+)"[^>]*type="model">.*?</object>',
        re.DOTALL,
    )
    def repl(m: re.Match) -> str:
        return "" if m.group(1) in drops else m.group(0)
    new = pattern.sub(repl, model_xml)
    return collapse_blank_lines(new)


def remove_model_settings_objects(cfg_xml: str, drops: set[str]) -> str:
    """Remove <object id="X">...</object> wrapper blocks (with their inner <part>)."""
    pattern = re.compile(
        r'\s*<object id="(\d+)">\s*<metadata key="name".*?</object>',
        re.DOTALL,
    )
    def repl(m: re.Match) -> str:
        return "" if m.group(1) in drops else m.group(0)
    new = pattern.sub(repl, cfg_xml)
    return collapse_blank_lines(new)


def remove_model_instances(cfg_xml: str, drops: set[str]) -> str:
    """Remove <model_instance>...</model_instance> blocks whose object_id is dropped."""
    pattern = re.compile(
        r'\s*<model_instance>\s*<metadata key="object_id" value="(\d+)"/>.*?</model_instance>',
        re.DOTALL,
    )
    def repl(m: re.Match) -> str:
        return "" if m.group(1) in drops else m.group(0)
    new = pattern.sub(repl, cfg_xml)
    return collapse_blank_lines(new)


def remove_assemble_items(cfg_xml: str, drops: set[str]) -> str:
    pattern = re.compile(r'\s*<assemble_item object_id="(\d+)"[^/]*/>')
    def repl(m: re.Match) -> str:
        return "" if m.group(1) in drops else m.group(0)
    new = pattern.sub(repl, cfg_xml)
    return collapse_blank_lines(new)


def collapse_blank_lines(s: str) -> str:
    return re.sub(r"(\r?\n)[ \t]*(?=\r?\n)", r"\1", s)


def rewrite_zip(src: Path, dst: Path, replacements: dict[str, bytes]) -> None:
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(
        dst, "w", zipfile.ZIP_DEFLATED
    ) as zout:
        for info in zin.infolist():
            data = replacements.get(info.filename) or zin.read(info.filename)
            zout.writestr(info, data)


def validate_xml(label: str, data: bytes) -> None:
    try:
        ET.fromstring(data)
    except ET.ParseError as e:
        raise SystemExit(f"XML parse error in {label}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", type=Path, help="3MF file to dedup (modified in place unless --out)")
    ap.add_argument("--out", type=Path, help="Write to this file instead of modifying in place")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with zipfile.ZipFile(args.path) as z:
        model_xml = z.read("3D/3dmodel.model").decode("utf-8")
        cfg_xml = z.read("Metadata/model_settings.config").decode("utf-8")

    name_by_resid = parse_name_by_resid(cfg_xml)
    drops = pick_drops(model_xml, name_by_resid)
    drops_set = set(drops)

    # Diagnostic
    items = ITEM_RE.findall(model_xml)
    by_name: dict[str, list[str]] = defaultdict(list)
    for oid, tr in items:
        by_name[name_by_resid.get(oid, f"?{oid}")].append(tr)
    print(f"Build items before: {len(items)}")
    print(f"Will drop: {len(drops)} items")
    stacked = sorted(
        ((n, len(t), len(set(t))) for n, t in by_name.items() if len(t) != len(set(t))),
        key=lambda x: -(x[1] - x[2]),
    )
    for name, total, uniq in stacked:
        print(f"  {name:40s} total={total:3d} unique_xforms={uniq:3d} drops={total - uniq}")

    new_model = remove_resource_objects(remove_build_items(model_xml, drops_set), drops_set)
    new_cfg = remove_assemble_items(
        remove_model_instances(
            remove_model_settings_objects(cfg_xml, drops_set), drops_set
        ),
        drops_set,
    )

    # Sanity: counts after
    new_items = ITEM_RE.findall(new_model)
    new_resources = re.findall(r'<object id="\d+"[^>]*type="model"', new_model)
    new_mi = re.findall(r'<model_instance>\s*<metadata key="object_id"', new_cfg)
    new_ai = re.findall(r'<assemble_item object_id="\d+"', new_cfg)
    new_ms_obj = re.findall(r'<object id="\d+">\s*<metadata key="name"', new_cfg)
    expected = len(items) - len(drops)
    print(
        f"\nAfter dedup — build items: {len(new_items)}, resources: {len(new_resources)}, "
        f"model_instances: {len(new_mi)}, assemble_items: {len(new_ai)}, "
        f"model_settings objects: {len(new_ms_obj)}"
    )
    print(f"Expected count for all five: {expected}")
    if not (
        len(new_items) == len(new_resources) == len(new_mi) == len(new_ai)
        == len(new_ms_obj) == expected
    ):
        raise SystemExit("FAIL: post-dedup counts do not match. Aborting.")

    # XML validation
    validate_xml("3dmodel.model", new_model.encode("utf-8"))
    validate_xml("model_settings.config", new_cfg.encode("utf-8"))
    print("XML well-formed. Counts consistent.")

    if args.dry_run:
        print("(dry run — no file written)")
        return 0

    out = args.out or args.path
    tmp = out.with_suffix(out.suffix + ".tmp")
    rewrite_zip(
        args.path,
        tmp,
        {
            "3D/3dmodel.model": new_model.encode("utf-8"),
            "Metadata/model_settings.config": new_cfg.encode("utf-8"),
        },
    )
    # ZIP integrity check
    with zipfile.ZipFile(tmp) as z:
        bad = z.testzip()
        if bad:
            raise SystemExit(f"ZIP integrity failure on {bad}")
    shutil.move(tmp, out)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
