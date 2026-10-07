#!/usr/bin/env python3
"""Map STL file paths to the 3MF file(s) that contain them.

A 3MF is listed only if update_3mf.py would actually pull its part from this
exact STL, using the same variant search rules (touch -> touch + base,
lite -> lite + base, joint-sensing -> joint-sensing + base, base -> base).
So a base STL cascades into every variant 3MF that uses it, while a variant
STL (or a variant's own copy of a base name) never flags another variant.

Usage:
  python find_3mf_for_file.py orca_v2/base/05_Spools/BaseSpool.stl
  # BaseSpool.stl -> orca_v2/base/Prints-1100.3mf
  # BaseSpool.stl -> orca_v2/touch/Prints-2100.3mf
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from update_3mf import find_stl_file, get_search_dirs  # noqa: E402

MODEL_ROOTS = ["orca_v1", "orca_v2"]


def get_model_root(filepath: str) -> str | None:
    """Resolve the hand model root directory from a file path."""
    p = Path(filepath)
    for part in p.parts:
        if part in MODEL_ROOTS:
            idx = p.parts.index(part)
            return str(Path(*p.parts[:idx + 1]))
    return None


def get_3mf_part_names(threemf_path: str) -> list[str]:
    """Extract part names from a 3MF's model_settings.config."""
    try:
        with zipfile.ZipFile(threemf_path, "r") as zf:
            config_xml = zf.read("Metadata/model_settings.config").decode("utf-8")
    except (zipfile.BadZipFile, KeyError):
        return []

    root = ET.fromstring(config_xml)
    names = []
    for obj in root.findall("object"):
        for part in obj.findall("part"):
            if part.get("subtype") == "normal_part":
                for meta in part.findall("metadata"):
                    if meta.get("key") == "name":
                        names.append(meta.get("value"))
                        break
    return names


def find_3mfs_in_model(model_root: str) -> list[str]:
    """Find all 3MF files within a model directory."""
    return [str(p) for p in Path(model_root).rglob("*.3mf")]


def find_3mf_for_files(stl_paths: list[str]) -> dict[str, list[str]]:
    """Map STL filenames to the 3MF(s) containing them.

    Returns dict of {stl_filename: [threemf_path, ...]}
    """
    # Group STL paths by model root
    stls_by_model: dict[str, list[str]] = {}
    for stl_path in stl_paths:
        model_root = get_model_root(stl_path)
        if model_root:
            stls_by_model.setdefault(model_root, []).append(stl_path)

    # Cache: model_root -> {3mf_path: [part_names]}
    model_3mf_cache: dict[str, dict[str, list[str]]] = {}
    # Cache: (3mf_path, part_name) -> STL that update_3mf.py would use
    resolved: dict[tuple[str, str], str | None] = {}

    results: dict[str, list[str]] = {}

    for model_root, stl_paths_in_model in stls_by_model.items():
        if model_root not in model_3mf_cache:
            threemf_files = find_3mfs_in_model(model_root)
            model_3mf_cache[model_root] = {
                tmf: get_3mf_part_names(tmf) for tmf in threemf_files
            }

        for stl_path in stl_paths_in_model:
            stl_name = Path(stl_path).name
            target = Path(stl_path).resolve()
            matches = []
            for tmf_path, part_names in model_3mf_cache[model_root].items():
                if stl_name not in part_names:
                    continue
                key = (tmf_path, stl_name)
                if key not in resolved:
                    search_dirs = get_search_dirs(tmf_path)
                    resolved[key] = find_stl_file(stl_name, search_dirs) if search_dirs else None
                if resolved[key] and Path(resolved[key]).resolve() == target:
                    matches.append(tmf_path)
            # Same name changed in two variants (e.g. base + touch Coav-R.stl): merge
            results[stl_name] = sorted(set(results.get(stl_name, [])) | set(matches))

    return results


def main():
    parser = argparse.ArgumentParser(description="Find which 3MF files contain a given STL")
    parser.add_argument("files", nargs="+", help="STL file paths to look up")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    results = find_3mf_for_files(args.files)

    if args.json:
        json.dump(results, sys.stdout, indent=2)
        print()
    else:
        for stl_name, threemf_paths in results.items():
            if threemf_paths:
                for tmf in threemf_paths:
                    print(f"{stl_name} -> {tmf}")
            else:
                print(f"{stl_name} -> (not found in any 3MF)")


if __name__ == "__main__":
    main()
