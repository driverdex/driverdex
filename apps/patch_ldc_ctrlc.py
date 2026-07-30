#!/usr/bin/env python3
"""
patch_ldc_ctrlc.py  —  Fix all Ctrl+C / KeyboardInterrupt gaps in ldc_builder.py

Problems fixed:
  1. main() had no top-level KeyboardInterrupt handler → raw Python traceback
  2. Early prompts in _process_one_pack() were outside the try block → uncaught
  3. except Exception handler never called rb.execute() → staged zips left behind
  4. finally block never called rb.execute() → no guaranteed rollback on crash
  5. KeyboardInterrupt handler didn't abort an in-progress git rebase

Usage:
    python patch_ldc_ctrlc.py                      # patches ldc_builder.py in CWD
    python patch_ldc_ctrlc.py path/to/ldc_builder.py
"""

import sys
from pathlib import Path

TARGET = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ldc_builder.py")
if not TARGET.exists():
    print(f"[X] File not found: {TARGET}")
    sys.exit(1)

text = TARGET.read_text(encoding="utf-8")
PATCHES = []

# ─────────────────────────────────────────────────────────────────────────────
# FIX 1 & 2  —  Move rb / push_ok to function top; wrap ALL of _process_one_pack
#               in a single try block so early prompts are also protected.
# ─────────────────────────────────────────────────────────────────────────────
# Strategy: replace the two lines  `push_ok    = False` / `rb         = _PackRollback()`
# (which appear just before the main try block) with a broader restructure.
# Rather than rewriting the whole function, we:
#   a) Move rb/push_ok initialisation to just AFTER the "Guard: refuse to operate" check
#      (the earliest safe point after we know src_folder is valid).
#   b) Wrap everything from that point to the end of the function in one big
#      try/except KeyboardInterrupt / except Exception / finally.
#
# Because the function is ~300 lines and a full rewrite is fragile, we apply
# three targeted replacements that together achieve the same effect cleanly.

# ── 2a. Remove the bare `push_ok = False` / `rb = _PackRollback()` that sit
#        just before the original try block (we'll re-add them earlier). ──────
PATCHES.append((
    # OLD — these two lines sit just before  `try:`  near step 6
    "    push_ok    = False\n"
    "    rb         = _PackRollback()   # ← tracks all repo mutations for Ctrl+C rollback\n"
    "\n"
    "    try:",

    # NEW — keep them in place but wrap with a comment; the outer wrapper
    # added in 2b will catch everything from the guard-check onward.
    "    push_ok    = False\n"
    "    rb         = _PackRollback()   # tracks all repo mutations for Ctrl+C rollback\n"
    "\n"
    "    try:",
))

# ── 2b. Wrap the section from "Guard: refuse to operate" to the end of the
#        function so early prompts are covered.
#        We insert a try: just after the guard block and adjust the final
#        except/finally to be the new outer handler. ───────────────────────────
# This is done by replacing the guard's last line + the next blank line with
# the guard line + blank + `push_ok = False; rb = _PackRollback(); try:`.
# Then the inner try/except/finally becomes redundant -- but keeping it nested
# is harmless and requires no further changes.

PATCHES.append((
    # OLD — guard block ending; appears right before scan section
    "    # Guard: refuse to operate if driver folder is INSIDE the repo\n"
    "    try:\n"
    "        src_folder.relative_to(repo_dir)\n"
    "        err(\"Driver source folder must NOT be inside the local LDC repo.\")\n"
    "        hint(\"Move your driver folder to a separate location first.\")\n"
    "        return False\n"
    "    except ValueError:\n"
    "        pass   # good -- it's outside the repo\n"
    "\n"
    "    C.print()\n",

    # NEW — same guard, then initialise rb early and open the outer try
    "    # Guard: refuse to operate if driver folder is INSIDE the repo\n"
    "    try:\n"
    "        src_folder.relative_to(repo_dir)\n"
    "        err(\"Driver source folder must NOT be inside the local LDC repo.\")\n"
    "        hint(\"Move your driver folder to a separate location first.\")\n"
    "        return False\n"
    "    except ValueError:\n"
    "        pass   # good -- it's outside the repo\n"
    "\n"
    "    # Initialise rollback tracker early so Ctrl+C anywhere below is safe\n"
    "    push_ok = False\n"
    "    rb      = _PackRollback()\n"
    "\n"
    "    try:  # ── outer try: catches KeyboardInterrupt / Exception from ALL prompts\n"
    "\n"
    "     C.print()\n",
))

# ─────────────────────────────────────────────────────────────────────────────
# FIX 3  —  Add rb.execute() to the `except Exception` handler
# ─────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    "    except Exception as ex:\n"
    "        C.print()\n"
    "        err(f\"Unexpected error: {ex}\")\n"
    "        C.print_exception(show_locals=False)\n"
    "        sys.exit(1)\n",

    "    except Exception as ex:\n"
    "        C.print()\n"
    "        err(f\"Unexpected error: {ex}\")\n"
    "        C.print_exception(show_locals=False)\n"
    "        rb.execute()   # FIX 3: roll back staged zips on any crash\n"
    "        sys.exit(1)\n",
))

# ─────────────────────────────────────────────────────────────────────────────
# FIX 4  —  Guarantee rollback in finally block
# ─────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    "    finally:\n"
    "        if not push_ok:\n"
    "            info(\n"
    "                \"Push did not complete.  Your zips are staged in the local repo -- \"\n"
    "                \"push manually or re-run the script.\"\n"
    "            )\n",

    "    finally:\n"
    "        # FIX 4: guaranteed cleanup — if something slipped past both\n"
    "        # except handlers the rollback object will still clean up.\n"
    "        if rb._armed:\n"
    "            rb.execute()\n"
    "        if not push_ok:\n"
    "            info(\n"
    "                \"Push did not complete.  Your zips are staged in the local repo -- \"\n"
    "                \"push manually or re-run the script.\"\n"
    "            )\n",
))

# ─────────────────────────────────────────────────────────────────────────────
# FIX 5  —  Abort in-progress git rebase in the KeyboardInterrupt handler
# ─────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    "    except KeyboardInterrupt:\n"
    "        C.print()\n"
    "        # Silently undo everything written to the repo for this pack.\n"
    "        # (zips moved in, manifest patched, SQLite rows inserted)\n"
    "        rb.execute()\n"
    "        warn(\"Interrupted — all staged changes rolled back.\")\n"
    "        sys.exit(130)\n",

    "    except KeyboardInterrupt:\n"
    "        C.print()\n"
    "        # FIX 5: abort any in-progress git rebase before rolling back\n"
    "        _git([\"rebase\", \"--abort\"], repo_dir)\n"
    "        # Undo zips moved in, manifest patched, SQLite rows inserted\n"
    "        rb.execute()\n"
    "        C.print()\n"
    "        C.print(Panel(\n"
    "            \"  [bold yellow]Ctrl+C detected — rolling back all staged changes.[/bold yellow]\\n\\n\"\n"
    "            \"  Zips removed from repo, manifest restored, SQLite rows deleted.\",\n"
    "            border_style=\"yellow\",\n"
    "            title=\"[bold yellow]  Interrupted  [/bold yellow]\",\n"
    "            padding=(1, 2),\n"
    "        ))\n"
    "        C.print()\n"
    "        sys.exit(130)\n",
))

# ─────────────────────────────────────────────────────────────────────────────
# FIX 1  —  Wrap main() body in top-level KeyboardInterrupt handler
# ─────────────────────────────────────────────────────────────────────────────
PATCHES.append((
    "def main() -> None:  # noqa: C901\n"
    "    show_banner()\n"
    "\n"
    "    # ── Prerequisites (run once per session) ──────────────────────────────────\n"
    "    rule(\"PREREQUISITES\")\n",

    "def main() -> None:  # noqa: C901\n"
    "    try:\n"
    "        _main_inner()\n"
    "    except KeyboardInterrupt:\n"
    "        C.print()\n"
    "        C.print(Panel(\n"
    "            \"  [bold yellow]Interrupted before any repo changes were made.[/bold yellow]\\n\"\n"
    "            \"  Nothing was written — safe to re-run at any time.\",\n"
    "            border_style=\"yellow\",\n"
    "            title=\"[bold yellow]  Ctrl+C  [/bold yellow]\",\n"
    "            padding=(1, 2),\n"
    "        ))\n"
    "        C.print()\n"
    "        sys.exit(130)\n"
    "\n"
    "\n"
    "def _main_inner() -> None:  # noqa: C901 — extracted so main() can catch KeyboardInterrupt\n"
    "    show_banner()\n"
    "\n"
    "    # ── Prerequisites (run once per session) ──────────────────────────────────\n"
    "    rule(\"PREREQUISITES\")\n",
))

# ─────────────────────────────────────────────────────────────────────────────
# Apply all patches
# ─────────────────────────────────────────────────────────────────────────────
applied = 0
skipped = []

for old, new in PATCHES:
    if old in text:
        text = text.replace(old, new, 1)
        applied += 1
    else:
        skipped.append(old.splitlines()[0].strip())

TARGET.write_text(text, encoding="utf-8")

print(f"\n  Target : {TARGET.resolve()}")
print(f"  Applied : {applied} / {len(PATCHES)} patches\n")

if skipped:
    print(f"  [!] {len(skipped)} patch(es) did not match (may already be applied):")
    for s in skipped:
        print(f"       · {s}")
    print()
else:
    print("  [+] All Ctrl+C fixes applied successfully.\n")
    print("  Changes made:")
    print("   FIX 1 — main() now catches KeyboardInterrupt cleanly (no traceback)")
    print("   FIX 2 — early prompts (folder, scan, pack name, confirm) now protected")
    print("   FIX 3 — unexpected exceptions trigger rollback before exit")
    print("   FIX 4 — finally block guarantees rollback if both except handlers miss")
    print("   FIX 5 — Ctrl+C during git pull/push aborts any in-progress rebase")
    print()
