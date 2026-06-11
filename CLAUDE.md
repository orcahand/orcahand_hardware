# ORCA Hand Files

Repository containing CAD source files (STL) and Bambu Lab print files (3MF) for the ORCA robotic hand project.

## Repo Structure

Two top-level designs:

- **`orca_v2/`** — Current ORCA hand. One canonical base, three variants sharing it:
  - `orca_v2/base/` — Full hand. 3MFs at root (`Prints-1000-DX.3mf`, `Prints-1000-FT.3mf`, `Clips-Only.3mf`), STLs in `01_Fingers/`, `02_Carpals/`, `03_Wrist/`, `04_ForeArm/`, `05_Spools/`, `06_CNC/`, `07_Molds/`, `08_MoldsWithClips/`, `skin/`
  - `orca_v2/touch/` — Touch-sensor variant. 3MFs at root (`Prints-2000-DX.3mf`, `Prints-2000-FT.3mf`). Override STLs (`*-Touch.stl`) in `01_Fingers/`, `02_Carpals/`.
  - `orca_v2/lite/` — Lite variant. STL sources only (`Lite-*`) in `01_ForeArm/`, `02_Spools/`. No 3MFs yet.
  - `orca_v2/joint-sensing/` — Joint-sensing variant. STL sources only (`*JS*`) in `01_Fingers/`. No 3MFs yet.
- **`orca_v1/`** — Older V1 design (self-contained). 3MFs in `01_CAD/Print_Files_Bambu/`, STLs in sibling dirs under `01_CAD/` (`ORCA_Fingers/`, `ORCA_Tower/`, `ORCA_Spools/`, `ORCA_Molds/`, `ORCA_Misc/`).

### Conventions

- Part names inside 3MF files must match STL filenames exactly (e.g. `BaseSpool.stl` in a 3MF corresponds to `05_Spools/BaseSpool.stl` on disk).
- Scripts auto-detect the model root from any file path. For v2, the model root is always `orca_v2/` (not the variant subfolder) — so a variant 3MF resolves against the whole v2 tree and naturally picks up shared base STLs.
- Filenames don't collide across v2 variants (base has no suffix, touch uses `-Touch`, JS uses `JS_` prefix, lite uses `Lite-` prefix), so `rglob` from `orca_v2/` returns the right file for any part name.
- Editing a shared base STL (e.g. `orca_v2/base/02_Carpals/CORE-L.stl`) automatically updates every variant 3MF that references it.
- Known model roots: `orca_v1`, `orca_v2`.

## Scripts

All scripts live in `scripts/` and use only Python stdlib (no external deps).

### `scripts/update_3mf.py`
Replace STL mesh data inside Bambu Lab 3MF files without opening Bambu Studio. Parses the 3MF zip structure, matches part names to STL files, and rewrites the mesh XML in-place.

```bash
# List all parts in a 3MF
python3 scripts/update_3mf.py orca_v2/base/Prints-1000-DX.3mf --list

# Update all STLs in a 3MF (auto-finds STLs under model root)
python3 scripts/update_3mf.py orca_v2/base/Prints-1000-DX.3mf --all

# Dry run — show what would be updated without writing
python3 scripts/update_3mf.py orca_v2/base/Prints-1000-DX.3mf --all --dry-run

# Replace a single part
python3 scripts/update_3mf.py orca_v2/base/Prints-1000-DX.3mf --stl BaseSpool.stl

# Update a variant 3MF — finds variant STLs AND shared base STLs in one pass
python3 scripts/update_3mf.py orca_v2/touch/Prints-2000-DX.3mf --all

# Explicit STL search directory (overrides auto-detection)
python3 scripts/update_3mf.py orca_v2/base/Prints-1000-DX.3mf --all --stl-dir orca_v2/base/05_Spools/

# Batch mode — process all 3MFs in a directory
python3 scripts/update_3mf.py --batch --3mf-dir orca_v2/base/

# JSON output for programmatic use
python3 scripts/update_3mf.py orca_v2/base/Prints-1000-DX.3mf --all --json
```

When `--stl-dir` is omitted, the script resolves the model root from the 3MF path and searches downward. For any v2 path the model root is `orca_v2/`, so the search descends into base + every variant — that's what makes the shared-base setup work.

### `scripts/find_3mf_for_file.py`
Map STL file paths to the 3MF(s) that contain them. Within `orca_v2/`, all variant 3MFs are candidates — editing a shared base STL cascades into every variant 3MF that references it. `orca_v1/` 3MFs are isolated from v2.

```bash
# Base STL — cascades into base + every variant 3MF that references it
python3 scripts/find_3mf_for_file.py orca_v2/base/05_Spools/BaseSpool.stl
# BaseSpool.stl -> orca_v2/base/Prints-1000-DX.3mf
# BaseSpool.stl -> orca_v2/touch/Prints-2000-DX.3mf

# Multiple files with JSON output
python3 scripts/find_3mf_for_file.py --json orca_v2/base/05_Spools/BaseSpool.stl orca_v2/base/02_Carpals/CORE-L.stl
```

### `scripts/update-print-files.py`
End-to-end script that finds changed STL files, backs up affected 3MFs, updates them, and optionally commits/pushes with git. Detects changes by comparing STL file modification times against `.last_update_timestamp` (no git dependency for change detection). Git operations (commit, push) are best-effort and won't block the script if git is unavailable or credentials don't work. Calls `find_3mf_for_file.py` and `update_3mf.py` under the hood.

```bash
# Preview what would change (safe, no modifications)
python3 scripts/update-print-files.py --dry-run

# Update changed STLs (no git operations by default)
python3 scripts/update-print-files.py

# Update, commit, and push (git auto-enabled for 'ccc' user, or use --git)
python3 scripts/update-print-files.py --git

# Re-sync ALL parts in affected 3MFs (not just changed ones)
python3 scripts/update-print-files.py --all

# Full sync: re-sync ALL parts in ALL 3MFs regardless of changes
python3 scripts/update-print-files.py --full-sync

# Commit but don't push
python3 scripts/update-print-files.py --git --no-push

# Combine
python3 scripts/update-print-files.py --full-sync --dry-run
```

### `scripts/sync.js` / `scripts/sync-changelog.js`
Firebase sync for distributing files. Run with `npm run sync` or `npm run sync-all`.

## Skills

### `/update-print-files`
End-to-end workflow: fetches latest from remote, finds changed STL files, updates affected 3MF files, commits with a descriptive message, and pushes to the current branch. Pass `--dry-run` to preview without changes, `--all` to re-sync every part, `--no-push` to skip pushing.

## 3MF Internal Structure

Bambu Lab 3MF files are ZIP archives with this relevant structure:
- `Metadata/model_settings.config` — XML listing all objects/parts with names and subtypes
- `3D/3dmodel.model` — XML with component references pointing to individual object model files
- `3D/Objects/object_N.model` — per-part XML containing the actual mesh (vertices + triangles)

The update script replaces the mesh XML inside the per-part object files while preserving all other metadata (transforms, print settings, plate assignments, etc.).
