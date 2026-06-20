#!/usr/bin/env python3
"""Replace STL mesh components inside Bambu Lab 3MF files.

Usage:
  # Replace a single STL inside a 3MF
  python update_3mf.py ORCA_Tower.3mf --stl BottomTower.stl --stl-dir ORCA_Tower/

  # Update ALL matching STLs found in the 3MF from a directory tree
  python update_3mf.py ORCA_Tower.3mf --all --stl-dir .

  # Batch: update all 3MFs in a directory
  python update_3mf.py --batch --3mf-dir Print_Files_Bambu/ --stl-dir .

  # Dry run to see what would be updated
  python update_3mf.py ORCA_Tower.3mf --all --stl-dir . --dry-run
"""

from __future__ import annotations

import argparse
import io
import json
import os
import struct
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

MODEL_ROOTS = ["orca_v1", "orca_v2"]


def get_model_root(filepath: str) -> str | None:
    """Resolve the hand model root directory from a file path.

    E.g. 'orca_v2/base/Prints16.3mf' -> 'orca_v2/'
         'orca_v2/touch/Prints16.3mf' -> 'orca_v2/' (returns the v2 root,
         not the variant subfolder, so STL search descends into both
         the variant and orca_v2/base/ in a single rglob pass)
         'orca_v1/Print_Files_Bambu/ORCA_Tower.3mf' -> 'orca_v1/'
    """
    p = Path(filepath).resolve()
    for part in p.parts:
        if part in MODEL_ROOTS:
            idx = p.parts.index(part)
            return str(Path(*p.parts[:idx + 1]))
    return None

NS_3MF = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
NS_BAMBU = "http://schemas.bambulab.com/package/2021"
NS_PROD = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"


def parse_binary_stl(filepath: str) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse a binary STL file, returning deduplicated (vertices, triangles)."""
    with open(filepath, "rb") as f:
        header = f.read(80)
        num_tris = struct.unpack("<I", f.read(4))[0]

        # Check if it might be ASCII STL
        if header.lstrip().lower().startswith(b"solid") and num_tris == 0:
            raise ValueError(f"ASCII STL not supported yet: {filepath}")

        raw_verts = []
        for _ in range(num_tris):
            data = f.read(50)  # 12 normal + 36 verts + 2 attr
            vals = struct.unpack("<12fH", data)
            # Skip normal (vals[0:3]), read 3 vertices
            for i in range(3):
                base = 3 + i * 3
                raw_verts.append((vals[base], vals[base + 1], vals[base + 2]))

    # Deduplicate vertices using a dict for O(1) lookup
    vert_map: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []

    for tri_idx in range(num_tris):
        tri_verts = []
        for v in range(3):
            vert = raw_verts[tri_idx * 3 + v]
            if vert not in vert_map:
                vert_map[vert] = len(vertices)
                vertices.append(vert)
            tri_verts.append(vert_map[vert])
        triangles.append(tuple(tri_verts))

    return vertices, triangles


def parse_ascii_stl(filepath: str) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse an ASCII STL file."""
    with open(filepath, "r") as f:
        content = f.read()

    raw_verts = []
    for line in content.splitlines():
        line = line.strip()
        if line.startswith("vertex"):
            parts = line.split()
            raw_verts.append((float(parts[1]), float(parts[2]), float(parts[3])))

    num_tris = len(raw_verts) // 3
    vert_map: dict[tuple[float, float, float], int] = {}
    vertices: list[tuple[float, float, float]] = []
    triangles: list[tuple[int, int, int]] = []

    for tri_idx in range(num_tris):
        tri_verts = []
        for v in range(3):
            vert = raw_verts[tri_idx * 3 + v]
            if vert not in vert_map:
                vert_map[vert] = len(vertices)
                vertices.append(vert)
            tri_verts.append(vert_map[vert])
        triangles.append(tuple(tri_verts))

    return vertices, triangles


def parse_stl(filepath: str) -> tuple[list[tuple[float, float, float]], list[tuple[int, int, int]]]:
    """Parse an STL file (binary or ASCII)."""
    with open(filepath, "rb") as f:
        header = f.read(80)

    if header.lstrip().lower().startswith(b"solid"):
        # Could be ASCII, but "solid" can also appear in binary headers.
        # Check if the file size matches binary format.
        file_size = os.path.getsize(filepath)
        num_tris = struct.unpack("<I", header[80 - 76:84 - 76] if len(header) >= 84 else b"\x00\x00\x00\x00")[0]

        # Re-read properly
        with open(filepath, "rb") as f:
            f.read(80)
            num_tris_bytes = f.read(4)
            if len(num_tris_bytes) == 4:
                num_tris = struct.unpack("<I", num_tris_bytes)[0]
                expected_size = 84 + num_tris * 50
                if file_size == expected_size:
                    return parse_binary_stl(filepath)

        # Try ASCII
        try:
            return parse_ascii_stl(filepath)
        except (ValueError, IndexError):
            return parse_binary_stl(filepath)

    return parse_binary_stl(filepath)


def center_mesh(
    vertices: list[tuple[float, float, float]],
) -> list[tuple[float, float, float]]:
    """Center mesh at bounding box center, matching Bambu Studio's import behavior."""
    xs = [v[0] for v in vertices]
    ys = [v[1] for v in vertices]
    zs = [v[2] for v in vertices]
    cx = (min(xs) + max(xs)) / 2
    cy = (min(ys) + max(ys)) / 2
    cz = (min(zs) + max(zs)) / 2
    return [(x - cx, y - cy, z - cz) for x, y, z in vertices]


def mesh_to_3mf_xml(
    vertices: list[tuple[float, float, float]],
    triangles: list[tuple[int, int, int]],
    object_id: str,
    object_uuid: str,
) -> str:
    """Convert mesh data to 3MF object model XML."""
    vert_lines = []
    for x, y, z in vertices:
        vert_lines.append(f'     <vertex x="{x}" y="{y}" z="{z}"/>')

    tri_lines = []
    for v1, v2, v3 in triangles:
        tri_lines.append(f'     <triangle v1="{v1}" v2="{v2}" v3="{v3}"/>')

    return f'''<?xml version="1.0" encoding="UTF-8"?>
<model unit="millimeter" xml:lang="en-US" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" xmlns:BambuStudio="http://schemas.bambulab.com/package/2021" xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06" requiredextensions="p">
 <metadata name="BambuStudio:3mfVersion">1</metadata>
 <resources>
  <object id="{object_id}" p:UUID="{object_uuid}" type="model">
   <mesh>
    <vertices>
{chr(10).join(vert_lines)}
    </vertices>
    <triangles>
{chr(10).join(tri_lines)}
    </triangles>
   </mesh>
  </object>
 </resources>
 <build/>
</model>'''


def get_3mf_part_mapping(zf: zipfile.ZipFile) -> dict[str, list[dict]]:
    """Parse a 3MF to get part name -> list of object file/ID mappings.

    Returns dict like:
      {"BottomTower.stl": [{"object_file": "3D/Objects/object_27.model",
                            "mesh_object_id": "1", "config_object_id": "3"}]}

    Parts with multiple instances (duplicates on the plate) will have multiple
    entries. Entries are deduplicated by object_file since instances sharing
    the same mesh file only need one update.
    """
    ET.register_namespace("", NS_3MF)
    ET.register_namespace("BambuStudio", NS_BAMBU)
    ET.register_namespace("p", NS_PROD)

    config_xml = zf.read("Metadata/model_settings.config").decode("utf-8")
    config_root = ET.fromstring(config_xml)

    # Collect ALL instances (same name can appear many times)
    name_config_entries: list[tuple[str, dict]] = []
    for obj in config_root.findall("object"):
        obj_id = obj.get("id")
        for part in obj.findall("part"):
            if part.get("subtype") == "normal_part":
                part_id = part.get("id")
                for meta in part.findall("metadata"):
                    if meta.get("key") == "name":
                        name_config_entries.append((
                            meta.get("value"),
                            {"config_object_id": obj_id, "part_id": part_id},
                        ))
                        break

    model_xml = zf.read("3D/3dmodel.model").decode("utf-8")
    model_root = ET.fromstring(model_xml)
    ns = {"m": NS_3MF, "p": NS_PROD}

    config_id_to_file = {}
    for obj in model_root.findall(".//m:resources/m:object", ns):
        obj_id = obj.get("id")
        components = obj.findall(".//m:component", ns)
        if components:
            first = components[0]
            path = first.get(f"{{{NS_PROD}}}path", "")
            mesh_obj_id = first.get("objectid", "")
            if path:
                config_id_to_file[obj_id] = {
                    "object_file": path.lstrip("/"),
                    "mesh_object_id": mesh_obj_id,
                }

    # Combine, deduplicating by object_file per name
    result: dict[str, list[dict]] = {}
    for name, info in name_config_entries:
        cid = info["config_object_id"]
        if cid in config_id_to_file:
            entry = {**config_id_to_file[cid], **info}
            if name not in result:
                result[name] = [entry]
            else:
                # Only add if it's a different object file
                seen_files = {e["object_file"] for e in result[name]}
                if entry["object_file"] not in seen_files:
                    result[name].append(entry)

    return result


def get_object_id_and_uuid(zf: zipfile.ZipFile, object_file: str, mesh_object_id: str) -> tuple[str, str]:
    """Read the existing object model file to get the object ID and UUID."""
    ns = {"m": NS_3MF, "p": NS_PROD}
    content = zf.read(object_file).decode("utf-8")
    root = ET.fromstring(content)
    for obj in root.findall(".//m:resources/m:object", ns):
        if obj.get("id") == mesh_object_id:
            uuid = obj.get(f"{{{NS_PROD}}}UUID", "")
            return mesh_object_id, uuid
    # Fallback: first object
    obj = root.find(".//m:resources/m:object", ns)
    if obj is not None:
        return obj.get("id", "1"), obj.get(f"{{{NS_PROD}}}UUID", "")
    return mesh_object_id, ""


def check_object_has_only_mesh(zf: zipfile.ZipFile, object_file: str) -> bool:
    """Check if an object model file contains only a single mesh object (safe to fully replace)."""
    ns = {"m": NS_3MF, "p": NS_PROD}
    content = zf.read(object_file).decode("utf-8")
    root = ET.fromstring(content)
    objects = root.findall(".//m:resources/m:object", ns)
    return len(objects) == 1


def replace_mesh_in_object_file(
    zf: zipfile.ZipFile,
    object_file: str,
    mesh_object_id: str,
    vertices: list[tuple[float, float, float]],
    triangles: list[tuple[int, int, int]],
) -> str:
    """Replace the mesh for a specific object ID within an object model file.

    Handles files with multiple objects (mesh + modifiers) by only replacing
    the target object's mesh.
    """
    ns = {"m": NS_3MF, "p": NS_PROD}
    content = zf.read(object_file).decode("utf-8")
    root = ET.fromstring(content)
    objects = root.findall(".//m:resources/m:object", ns)

    if len(objects) == 1:
        # Simple case: single object, generate fresh XML
        obj = objects[0]
        obj_id = obj.get("id", mesh_object_id)
        obj_uuid = obj.get(f"{{{NS_PROD}}}UUID", "")
        return mesh_to_3mf_xml(vertices, triangles, obj_id, obj_uuid)

    # Multiple objects: only replace the mesh of the target object
    for obj in objects:
        if obj.get("id") == mesh_object_id:
            mesh_elem = obj.find("m:mesh", ns)
            if mesh_elem is None:
                mesh_elem = obj.find("mesh")
            if mesh_elem is not None:
                obj.remove(mesh_elem)

            # Build new mesh element
            new_mesh = ET.SubElement(obj, f"{{{NS_3MF}}}mesh")
            verts_elem = ET.SubElement(new_mesh, f"{{{NS_3MF}}}vertices")
            for x, y, z in vertices:
                v = ET.SubElement(verts_elem, f"{{{NS_3MF}}}vertex")
                v.set("x", str(x))
                v.set("y", str(y))
                v.set("z", str(z))

            tris_elem = ET.SubElement(new_mesh, f"{{{NS_3MF}}}triangles")
            for v1, v2, v3 in triangles:
                t = ET.SubElement(tris_elem, f"{{{NS_3MF}}}triangle")
                t.set("v1", str(v1))
                t.set("v2", str(v2))
                t.set("v3", str(v3))
            break

    ET.register_namespace("", NS_3MF)
    ET.register_namespace("BambuStudio", NS_BAMBU)
    ET.register_namespace("p", NS_PROD)
    return ET.tostring(root, encoding="unicode", xml_declaration=True)


def find_stl_file(name: str, stl_dir: str) -> str | None:
    """Search for an STL file by name in a directory tree."""
    stl_dir = Path(stl_dir)
    # Exact match first
    for path in stl_dir.rglob(name):
        if path.is_file():
            return str(path)
    # Case-insensitive fallback
    name_lower = name.lower()
    for path in stl_dir.rglob("*.stl"):
        if path.name.lower() == name_lower:
            return str(path)
    return None


def update_3mf(
    threemf_path: str,
    stl_replacements: dict[str, str],
    dry_run: bool = False,
) -> list[str]:
    """Update a 3MF file, replacing specified STL meshes.

    Args:
        threemf_path: Path to the 3MF file
        stl_replacements: Dict of {part_name: stl_file_path}
        dry_run: If True, only report what would be changed

    Returns:
        List of replaced part names
    """
    replaced = []

    with zipfile.ZipFile(threemf_path, "r") as zf:
        mapping = get_3mf_part_mapping(zf)

        replacements_to_make = {}
        for part_name, stl_path in stl_replacements.items():
            if part_name not in mapping:
                print(f"  WARNING: '{part_name}' not found in {threemf_path}")
                continue

            entries = mapping[part_name]
            vertices = triangles = None

            for info in entries:
                obj_file = info["object_file"]
                mesh_id = info["mesh_object_id"]

                if dry_run:
                    print(f"  Would replace: {part_name} -> {obj_file} (object {mesh_id})")
                    continue

                if vertices is None:
                    vertices, triangles = parse_stl(stl_path)
                    vertices = center_mesh(vertices)

                new_xml = replace_mesh_in_object_file(zf, obj_file, mesh_id, vertices, triangles)
                replacements_to_make[obj_file] = new_xml
                print(f"  Replaced: {part_name} ({len(vertices)} verts, {len(triangles)} tris) -> {obj_file}")

            replaced.append(part_name)

        if dry_run or not replacements_to_make:
            return replaced

        # Rewrite the ZIP with replacements
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf_out:
            for item in zf.infolist():
                if item.filename in replacements_to_make:
                    zf_out.writestr(item, replacements_to_make[item.filename])
                else:
                    zf_out.writestr(item, zf.read(item.filename))

    # Write back
    with open(threemf_path, "wb") as f:
        f.write(buf.getvalue())

    return replaced


def list_parts(threemf_path: str):
    """List all parts in a 3MF file."""
    with zipfile.ZipFile(threemf_path, "r") as zf:
        mapping = get_3mf_part_mapping(zf)

    print(f"\nParts in {threemf_path}:")
    for name, entries in sorted(mapping.items()):
        files = [f"{e['object_file']} (obj {e['mesh_object_id']})" for e in entries]
        suffix = f" [{len(entries)} object files]" if len(entries) > 1 else ""
        print(f"  {name:30s} -> {files[0]}{suffix}")
        for f in files[1:]:
            print(f"  {'':30s}    {f}")
    if not mapping:
        print("  (no parts found)")


def main():
    parser = argparse.ArgumentParser(description="Update STL components in Bambu Lab 3MF files")
    parser.add_argument("threemf", nargs="?", help="Path to the 3MF file")
    parser.add_argument("--stl", nargs="+", help="Name(s) of STL(s) to replace (e.g. BaseSpool.stl Ratchet.stl)")
    parser.add_argument("--stl-dir", default=None, help="Directory to search for STL files (default: model root)")
    parser.add_argument("--all", action="store_true", help="Replace all matching STLs found in --stl-dir")
    parser.add_argument("--batch", action="store_true", help="Process all 3MF files in --3mf-dir")
    parser.add_argument("--3mf-dir", dest="threemf_dir", help="Directory containing 3MF files (for --batch)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be updated without changing files")
    parser.add_argument("--list", action="store_true", help="List parts in the 3MF file")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")

    args = parser.parse_args()

    if args.list:
        if not args.threemf:
            parser.error("--list requires a 3MF file")
        list_parts(args.threemf)
        return

    if args.batch:
        threemf_dir = args.threemf_dir or "."
        threemf_files = list(Path(threemf_dir).rglob("*.3mf"))
        if not threemf_files:
            print(f"No 3MF files found in {threemf_dir}")
            return

        total_replaced = 0
        json_results = {}
        for threemf_path in sorted(threemf_files):
            if not args.json:
                print(f"\n{'='*60}")
                print(f"Processing: {threemf_path}")

            stl_dir = args.stl_dir or get_model_root(str(threemf_path)) or "."

            with zipfile.ZipFile(str(threemf_path), "r") as zf:
                mapping = get_3mf_part_mapping(zf)

            stl_replacements = {}
            for part_name in mapping:
                stl_path = find_stl_file(part_name, stl_dir)
                if stl_path:
                    stl_replacements[part_name] = stl_path

            if stl_replacements:
                replaced = update_3mf(str(threemf_path), stl_replacements, dry_run=args.dry_run)
                total_replaced += len(replaced)
                if args.json:
                    json_results[str(threemf_path)] = replaced
            else:
                if not args.json:
                    print("  No matching STL files found")

        if args.json:
            json.dump(json_results, sys.stdout, indent=2)
            print()
        else:
            print(f"\n{'='*60}")
            action = "Would replace" if args.dry_run else "Replaced"
            print(f"{action} {total_replaced} parts across {len(threemf_files)} 3MF files")
        return

    if not args.threemf:
        parser.error("Please provide a 3MF file path (or use --batch)")

    stl_dir = args.stl_dir or get_model_root(args.threemf) or "."

    if args.all:
        with zipfile.ZipFile(args.threemf, "r") as zf:
            mapping = get_3mf_part_mapping(zf)

        if not args.json:
            print(f"Scanning for STL files in: {stl_dir}")
        stl_replacements = {}
        for part_name in mapping:
            stl_path = find_stl_file(part_name, stl_dir)
            if stl_path:
                stl_replacements[part_name] = stl_path
                if not args.json:
                    print(f"  Found: {part_name} -> {stl_path}")
            else:
                if not args.json:
                    print(f"  Not found: {part_name}")

        if stl_replacements:
            if not args.json:
                print(f"\nUpdating {args.threemf}...")
            replaced = update_3mf(args.threemf, stl_replacements, dry_run=args.dry_run)
            if args.json:
                json.dump({args.threemf: replaced}, sys.stdout, indent=2)
                print()
            else:
                action = "Would replace" if args.dry_run else "Replaced"
                print(f"\n{action} {len(replaced)} parts")
        else:
            if args.json:
                json.dump({args.threemf: []}, sys.stdout, indent=2)
                print()
            else:
                print("\nNo matching STL files found")
        return

    if args.stl:
        stl_replacements = {}
        for stl_name in args.stl:
            stl_path = find_stl_file(stl_name, stl_dir)
            if stl_path:
                stl_replacements[stl_name] = stl_path
                if not args.json:
                    print(f"  Found: {stl_name} -> {stl_path}")
            else:
                if not args.json:
                    print(f"  ERROR: Could not find '{stl_name}' in {stl_dir}")

        if stl_replacements:
            if not args.json:
                print(f"\nUpdating {args.threemf}...")
            replaced = update_3mf(args.threemf, stl_replacements, dry_run=args.dry_run)
            if args.json:
                json.dump({args.threemf: replaced}, sys.stdout, indent=2)
                print()
            else:
                action = "Would replace" if args.dry_run else "Replaced"
                print(f"\n{action} {len(replaced)} parts")
        return

    parser.error("Please specify --stl, --all, or --list")


if __name__ == "__main__":
    main()
