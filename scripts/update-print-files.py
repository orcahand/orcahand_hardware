#!/usr/bin/env python3
"""Find changed STL files, back up affected 3MFs, update them, and optionally commit/push.

Detects changed STLs by comparing file modification times against the last
successful run (stored in .last_update_timestamp). Git operations are OFF by
default (shared Google Drive causes lock conflicts) and enabled with --git.
Git is auto-enabled when running as the 'ccc' macOS user.

Usage:
  python3 scripts/update-print-files.py              # update 3MFs only (no git)
  python3 scripts/update-print-files.py --git         # update, commit, and push
  python3 scripts/update-print-files.py --all         # re-sync every part in affected 3MFs
  python3 scripts/update-print-files.py --full-sync   # re-sync ALL parts in ALL 3MFs
  python3 scripts/update-print-files.py --dry-run     # preview without writing
  python3 scripts/update-print-files.py --no-push     # commit but don't push (requires --git)
  python3 scripts/update-print-files.py --yes         # skip the confirmation prompt
"""

import getpass
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
MODEL_DIRS = ["orca_v2"]
TIMESTAMP_FILE = REPO_ROOT / ".last_update_timestamp"
BACKUP_ROOT = REPO_ROOT / ".backups"
KEEP_BACKUPS = 5  # number of most-recent backup snapshots to retain


def prune_backups(root=BACKUP_ROOT, keep=KEEP_BACKUPS):
    """Keep only the `keep` most recent timestamped backup dirs under root."""
    if not root.is_dir():
        return
    snaps = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.name)
    for old in snaps[:-keep]:
        shutil.rmtree(old, ignore_errors=True)


def log(msg=""):
    print(msg, flush=True)


def log_header(title):
    log(f"\n{'=' * 60}")
    log(f"  {title}")
    log(f"{'=' * 60}")


def log_step(n, title):
    log(f"\n--- Step {n}: {title} ---")


def confirm(prompt, default=True):
    """Ask a yes/no question. Returns default if non-interactive (no TTY)."""
    if not sys.stdin.isatty():
        return default
    suffix = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"\n  {prompt} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        log()
        return False
    if answer == "":
        return default
    return answer in ("y", "yes")


def run(cmd, timeout=None, **kwargs):
    log(f"  $ {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO_ROOT, timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        log(f"  WARNING: command timed out after {timeout}s: {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, returncode=1, stdout="", stderr="timed out")
    if result.stdout.strip():
        for line in result.stdout.strip().splitlines():
            log(f"    {line}")
    if result.stderr.strip():
        for line in result.stderr.strip().splitlines():
            log(f"    [stderr] {line}")
    return result


def git_available():
    """Check if git is installed and we're in a repo."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            capture_output=True, text=True, cwd=REPO_ROOT,
            timeout=10,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# Prevent git from hanging on interactive credential prompts
os.environ["GIT_TERMINAL_PROMPT"] = "0"


GIT_TIMEOUT = 30  # seconds — prevent git from hanging on credential prompts or slow networks

def git_run(cmd, **kwargs):
    """Run a git command best-effort. Returns result or None on failure."""
    kwargs.setdefault("timeout", GIT_TIMEOUT)
    try:
        return run(cmd, **kwargs)
    except FileNotFoundError:
        log(f"  WARNING: git not found, skipping: {' '.join(cmd)}")
        return None


def get_current_branch():
    """Get the current git branch name, or None if git unavailable."""
    r = git_run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if r and r.returncode == 0:
        return r.stdout.strip()
    return None


def fetch_and_pull():
    """Fetch latest changes from remote and pull current branch. Best-effort."""
    if not git_available():
        log("\n  Git not available, skipping fetch/pull")
        return None

    log_step(0, "Fetching latest changes from remote")
    branch = get_current_branch()
    if not branch:
        log("  WARNING: could not determine branch, skipping fetch/pull")
        return None
    log(f"  Current branch: {branch}")

    r = git_run(["git", "fetch", "origin", branch])
    if not r or r.returncode != 0:
        log(f"  WARNING: fetch failed, continuing anyway")
        return branch

    r = git_run(["git", "rev-list", "--count", f"HEAD..origin/{branch}"])
    behind = int(r.stdout.strip()) if r and r.stdout.strip().isdigit() else 0

    if behind > 0:
        log(f"  Branch is {behind} commit(s) behind origin/{branch}, pulling...")
        r = git_run(["git", "pull", "--rebase", "origin", branch])
        if not r or r.returncode != 0:
            log(f"  WARNING: pull failed, continuing with local state")
        else:
            log(f"  Pulled {behind} commit(s) successfully")
    else:
        log(f"  Already up to date with origin/{branch}")

    return branch


def get_last_update_time():
    """Read the last update timestamp. Returns 0.0 if no previous run."""
    if TIMESTAMP_FILE.exists():
        try:
            return float(TIMESTAMP_FILE.read_text().strip())
        except (ValueError, OSError):
            pass
    return 0.0


def save_update_time():
    """Save the current time as the last update timestamp."""
    try:
        TIMESTAMP_FILE.write_text(str(datetime.now().timestamp()))
    except OSError as e:
        log(f"  WARNING: could not save timestamp: {e}")


def find_changed_stls():
    """Find STL files modified since the last successful run."""
    last_update = get_last_update_time()
    stl_paths = set()

    if last_update == 0.0:
        log("  No previous update timestamp found — treating all STLs as new")

    for model_dir in MODEL_DIRS:
        model_path = REPO_ROOT / model_dir
        if not model_path.is_dir():
            continue
        for stl_file in model_path.rglob("*.stl"):
            if stl_file.stat().st_mtime > last_update:
                stl_paths.add(str(stl_file.relative_to(REPO_ROOT)))

    return sorted(stl_paths)


def find_3mfs_for_stls(stl_paths):
    """Map STL paths to 3MF files using find_3mf_for_file.py."""
    script = str(SCRIPT_DIR / "find_3mf_for_file.py")
    r = run(["python3", script, "--json"] + stl_paths)
    return json.loads(r.stdout)


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    update_all = "--all" in args
    full_sync = "--full-sync" in args
    no_push = "--no-push" in args
    assume_yes = "--yes" in args or "-y" in args
    # Git is off by default (shared Google Drive causes lock conflicts).
    # Auto-enable for the 'ccc' user, or explicitly with --git.
    use_git = "--git" in args or getpass.getuser() == "ccc"

    mode = "DRY RUN" if dry_run else "LIVE"
    if full_sync:
        scope = "full sync — all parts in all 3MFs"
    elif update_all:
        scope = "all parts in affected 3MFs"
    else:
        scope = "changed STLs only"
    log_header(f"update-print-files  [{mode}]  [{scope}]")
    log(f"  Repo root: {REPO_ROOT}")
    log(f"  Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    if use_git:
        log(f"  Git operations: enabled" + (" (auto: user=ccc)" if getpass.getuser() == "ccc" and "--git" not in args else ""))
    else:
        log(f"  Git operations: disabled (use --git to enable)")

    # Step 0: Fetch & pull latest changes (best-effort)
    if use_git and not dry_run:
        branch = fetch_and_pull()
    elif use_git and dry_run:
        branch = get_current_branch()
        if branch:
            log(f"\n  Current branch: {branch} (skipping fetch — dry run)")
        else:
            log(f"\n  Git not available (skipping fetch — dry run)")
    else:
        branch = None

    orphan_stls = []
    stl_path_by_name: dict[str, str] = {}

    if full_sync:
        # Find ALL 3MF files across all model directories
        log_step(1, "Finding all 3MF files")
        threemf_to_stls: dict[str, list[str]] = {}
        for model_dir in MODEL_DIRS:
            model_path = REPO_ROOT / model_dir
            if not model_path.is_dir():
                continue
            for tmf in sorted(model_path.rglob("*.3mf")):
                rel = str(tmf.relative_to(REPO_ROOT))
                threemf_to_stls[rel] = []  # --all flag on update_3mf.py will handle finding parts
                log(f"    {rel}")

        if not threemf_to_stls:
            log("\n  No 3MF files found.")
            log()
            return 0

        log(f"\n  Found {len(threemf_to_stls)} 3MF file(s) to sync")
        stl_paths = []

    else:
        # Step 1: Find changed STLs
        log_step(1, "Finding changed STL files")
        stl_paths = find_changed_stls()

        if not stl_paths:
            log("\n  No changed STL files found. Nothing to do.")
            log()
            return 0

        log(f"\n  Found {len(stl_paths)} changed STL file(s):")
        for p in stl_paths:
            log(f"    - {p}")

        # Step 2: Map STLs to 3MFs
        log_step(2, "Mapping STLs to 3MF files")
        stl_to_3mfs = find_3mfs_for_stls(stl_paths)

        # Build 3mf -> [stl_filenames] mapping and track orphans
        threemf_to_stls: dict[str, list[str]] = {}

        for stl_path in stl_paths:
            stl_name = Path(stl_path).name
            stl_path_by_name[stl_name] = stl_path
            matches = stl_to_3mfs.get(stl_name, [])
            if not matches:
                orphan_stls.append(stl_path)
                log(f"    {stl_name} -> (not in any 3MF)")
            else:
                for tmf in matches:
                    threemf_to_stls.setdefault(tmf, []).append(stl_name)
                    log(f"    {stl_name} -> {tmf}")

        if not threemf_to_stls:
            log("\n  No 3MF files need updating.")
            if orphan_stls:
                log(f"  ({len(orphan_stls)} STL(s) not found in any 3MF)")
            log()
            return 0

    log(f"\n  3MF files to update: {len(threemf_to_stls)}")
    for tmf, stls in sorted(threemf_to_stls.items()):
        if stls:
            log(f"    {tmf} ({len(stls)} part(s))")
        else:
            log(f"    {tmf} (all parts)")

    # Confirm before making any changes (skipped on dry run or with --yes)
    if not dry_run and not assume_yes:
        if not confirm("Proceed with updating these 3MF file(s)?", default=True):
            log("\n  Aborted — no changes made.")
            log()
            return 0

    # Ask whether to commit and push (only relevant when git is on and writing)
    if not dry_run and use_git:
        if assume_yes:
            commit_push = True
        else:
            commit_push = confirm("Commit and push the changes when done?", default=True)
        if not commit_push:
            use_git = False
            log("  Will update files only — skipping commit and push.")

    # Step 3: Back up 3MF files
    backup_dir = None
    if not dry_run:
        log_step(3, "Backing up 3MF files")
        timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        backup_dir = BACKUP_ROOT / timestamp

        for tmf in sorted(threemf_to_stls.keys()):
            src = REPO_ROOT / tmf
            dst = backup_dir / tmf
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            log(f"    Backed up: {tmf}")
            log(f"      -> {dst.relative_to(REPO_ROOT)}")

        prune_backups()
        log(f"\n  Backups saved to: .backups/{timestamp}/ (keeping last {KEEP_BACKUPS})")
    else:
        log_step(3, "Backing up 3MF files (skipped — dry run)")

    # Step 4: Update 3MF files
    log_step(4, "Updating 3MF files")
    update_script = str(SCRIPT_DIR / "update_3mf.py")
    results = {}
    tmf_status = {}  # track status per 3MF for full-sync summary

    for tmf in sorted(threemf_to_stls.keys()):
        stl_names = threemf_to_stls[tmf]
        log(f"\n  Updating: {tmf}")

        cmd = ["python3", update_script, tmf]
        if full_sync or update_all:
            cmd.append("--all")
        else:
            cmd.extend(["--stl"] + stl_names)
        if dry_run:
            cmd.append("--dry-run")

        r = run(cmd)

        if r.returncode != 0:
            log(f"  ERROR: update_3mf.py exited with code {r.returncode}")
            status = "error"
        else:
            status = "dry-run" if dry_run else "updated"

        tmf_status[tmf] = status

        for stl_name in stl_names:
            stl_path = stl_path_by_name.get(stl_name, stl_name)
            results.setdefault(stl_path, []).append({"3mf": tmf, "status": status})

    if full_sync:
        for tmf in sorted(threemf_to_stls.keys()):
            results.setdefault(tmf, []).append({"3mf": tmf, "status": tmf_status.get(tmf, "unknown")})

    for stl_path in orphan_stls:
        results.setdefault(stl_path, []).append({"3mf": "—", "status": "not in any 3MF"})

    # Step 5: Git add (best-effort)
    has_git = use_git and git_available()
    paths_to_stage = []
    if not dry_run and has_git:
        log_step(5, "Staging files with git add")
        paths_to_stage = list(stl_paths) + list(threemf_to_stls.keys()) if not full_sync else list(threemf_to_stls.keys())
        paths_to_stage = [p for p in paths_to_stage if (REPO_ROOT / p).exists()]
        if paths_to_stage:
            git_run(["git", "add"] + paths_to_stage)
            log(f"\n  Staged {len(paths_to_stage)} file(s)")
        else:
            log("  No files to stage")
    elif not dry_run and not has_git:
        log_step(5, "Staging files (skipped — git disabled)")
    else:
        log_step(5, "Staging files (skipped — dry run)")

    # Step 6: Commit (best-effort)
    committed = False
    if not dry_run and has_git and paths_to_stage:
        log_step(6, "Committing changes")
        if full_sync:
            commit_msg = f"Full sync: update all parts in {len(threemf_to_stls)} 3MF print file(s)"
        else:
            stl_names = [Path(p).name for p in stl_paths]
            if len(stl_names) <= 5:
                parts_list = ", ".join(stl_names)
            else:
                parts_list = ", ".join(stl_names[:5]) + f", and {len(stl_names) - 5} more"
            commit_msg = f"Update {parts_list} in 3MF print files"
        r = git_run(["git", "commit", "-m", commit_msg])
        if r and r.returncode == 0:
            log(f"  Committed: {commit_msg}")
            committed = True
        else:
            log("  WARNING: commit failed, continuing anyway")
    elif dry_run:
        log_step(6, "Committing (skipped — dry run)")
    elif not has_git:
        log_step(6, "Committing (skipped — git disabled)")

    # Step 7: Push (best-effort)
    if not dry_run and not no_push and has_git and committed:
        branch = branch or get_current_branch()
        if branch:
            log_step(7, f"Pushing to origin/{branch}")
            r = git_run(["git", "push", "origin", branch])
            if not r or r.returncode != 0:
                log("  WARNING: push failed (credentials or network issue), continuing anyway")
            else:
                log(f"  Pushed to origin/{branch}")
        else:
            log_step(7, "Pushing (skipped — could not determine branch)")
    elif dry_run:
        log_step(7, "Pushing (skipped — dry run)")
    elif no_push:
        log_step(7, "Pushing (skipped — --no-push)")
    elif not has_git:
        log_step(7, "Pushing (skipped — git disabled)")

    # Step 8: Summary
    log_header("Summary")

    if results:
        first_col_label = "3MF file" if full_sync else "STL file"
        first_col = max(len(first_col_label), max(len(p) for p in results))
        tmf_col = max(len("3MF file(s)"), max(len(e["3mf"]) for entries in results.values() for e in entries))
        stat_col = max(len("Status"), max(len(e["status"]) for entries in results.values() for e in entries))

        header = f"| {first_col_label:<{first_col}} | {'3MF file(s)':<{tmf_col}} | {'Status':<{stat_col}} |"
        sep = f"|-{'-' * first_col}-|-{'-' * tmf_col}-|-{'-' * stat_col}-|"
        log(header)
        log(sep)

        for key in sorted(results.keys()):
            for i, entry in enumerate(results[key]):
                display = key if i == 0 else ""
                log(f"| {display:<{first_col}} | {entry['3mf']:<{tmf_col}} | {entry['status']:<{stat_col}} |")

    total_updated = sum(1 for entries in results.values() for e in entries if e["status"] == "updated")
    total_dry = sum(1 for entries in results.values() for e in entries if e["status"] == "dry-run")
    total_orphan = sum(1 for entries in results.values() for e in entries if e["status"] == "not in any 3MF")
    total_error = sum(1 for entries in results.values() for e in entries if e["status"] == "error")

    if full_sync:
        log(f"\n  Total: {len(threemf_to_stls)} 3MF(s)")
    else:
        log(f"\n  Total: {len(stl_paths)} STL(s), {len(threemf_to_stls)} 3MF(s)")
    if total_updated:
        log(f"    Updated: {total_updated}")
    if total_dry:
        log(f"    Would update (dry run): {total_dry}")
    if total_orphan:
        log(f"    Not in any 3MF: {total_orphan}")
    if total_error:
        log(f"    Errors: {total_error}")

    if backup_dir:
        timestamp = backup_dir.name
        log(f"\n  Backups saved to: .backups/{timestamp}/")
        log(f"  To restore: cp .backups/{timestamp}/<model>/<file>.3mf <model>/<file>.3mf")

    if not dry_run and not total_error:
        if committed:
            log(f"\n  Changes committed" + (f" and pushed to origin/{branch}" if not no_push and has_git else ""))
        else:
            log(f"\n  3MF files updated successfully")
        # Save timestamp so next run only picks up newer STLs
        save_update_time()
        log(f"  Updated timestamp: {TIMESTAMP_FILE.relative_to(REPO_ROOT)}")

    log()
    return 1 if total_error else 0


if __name__ == "__main__":
    sys.exit(main())
