Update 3MF print files to match changed STL files, commit, and push.

Arguments: $ARGUMENTS (optional: --dry-run to preview, --all to re-sync every part in affected 3MFs, --no-push to skip pushing)

Steps:

1. Run `python3 scripts/update-print-files.py $ARGUMENTS` from the repo root. The script handles everything end-to-end:
   - Fetches latest changes from the current branch and pulls (with rebase)
   - Finds changed/untracked STL files via git
   - Maps them to 3MF files using `find_3mf_for_file.py`
   - Backs up affected 3MFs to `.backups/<timestamp>/`
   - Updates the 3MF files with `update_3mf.py`
   - Stages all changed STL and 3MF files
   - Commits with a descriptive message listing the updated parts
   - Pushes to the current branch on origin

2. Show the user the full script output, which includes a summary table and status of each step.

3. If the script exits with a non-zero code, report the error and suggest the user check the output for details.
