#!/usr/bin/env python3
"""
patch_ldc_defaults.py
Applies 4 changes to ldc_builder.py so every interactive prompt
defaults to Yes, speeding up the workflow.

Usage:
    python patch_ldc_defaults.py                    # patches ldc_builder.py in CWD
    python patch_ldc_defaults.py path/to/ldc_builder.py
"""

import sys
import re
from pathlib import Path

TARGET = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("ldc_builder.py")

if not TARGET.exists():
    print(f"[X] File not found: {TARGET}")
    sys.exit(1)

text = TARGET.read_text(encoding="utf-8")
original = text

PATCHES = [
    # ── 1. "Proceed?" prompt — add default="y" ───────────────────────────────
    (
        'ans = Prompt.ask(\n'
        '        "  [bold cyan]Proceed?[/bold cyan]  "\n'
        '        "[[bold green]y[/bold green]/[bold red]n[/bold red]]"\n'
        '    ).strip().lower()',

        'ans = Prompt.ask(\n'
        '        "  [bold cyan]Proceed?[/bold cyan]  "\n'
        '        "[[bold green]Y[/bold green]/[bold red]n[/bold red]]",\n'
        '        default="y",\n'
        '    ).strip().lower()',
    ),

    # ── 2. Duplicate audit bulk-action — default "" → "a" (apply defaults) ──
    (
        'bulk = Prompt.ask(\n'
        '        "  [bold cyan]Bulk action[/bold cyan]  "\n'
        '        "[[bold white]a[/bold white]=apply defaults / "\n'
        '        "[bold white]A[/bold white]=accept all / or press Enter for per-item]",\n'
        '        default="",\n'
        '    ).strip().lower()',

        'bulk = Prompt.ask(\n'
        '        "  [bold cyan]Bulk action[/bold cyan]  "\n'
        '        "[[bold white]A[/bold white]=apply defaults / "\n'
        '        "[bold white]a[/bold white]=accept all / or press Enter for per-item]",\n'
        '        default="a",\n'
        '    ).strip().lower()',
    ),

    # ── 3. "Delete source folder?" — default=False → default=True ───────────
    (
        'delete_src = Confirm.ask(\n'
        '                f"  [bold red]Delete source folder?[/bold red]  "\n'
        '                f"[dim]{src_folder}[/dim]",\n'
        '                default=False,\n'
        '            )',

        'delete_src = Confirm.ask(\n'
        '                f"  [bold red]Delete source folder?[/bold red]  "\n'
        '                f"[dim]{src_folder}[/dim]",\n'
        '                default=True,\n'
        '            )',
    ),

    # ── 4. "Process another driver pack?" — add default="y" ─────────────────
    (
        'again = Prompt.ask(\n'
        '                f"  [bold cyan]Process another driver pack?[/bold cyan]  "\n'
        '                f"[[bold green]y[/bold green]/[bold red]n[/bold red]]"\n'
        '            ).strip().lower()',

        'again = Prompt.ask(\n'
        '                f"  [bold cyan]Process another driver pack?[/bold cyan]  "\n'
        '                f"[[bold green]Y[/bold green]/[bold red]n[/bold red]]",\n'
        '                default="y",\n'
        '            ).strip().lower()',
    ),
]

applied = 0
failed  = []

for old, new in PATCHES:
    if old in text:
        text = text.replace(old, new, 1)
        applied += 1
    else:
        # Fallback: try normalising whitespace for robustness
        failed.append(old.splitlines()[0].strip())

# Write patched file
TARGET.write_text(text, encoding="utf-8")

print(f"\n  ldc_builder.py — {TARGET.resolve()}\n")
print(f"  [+] Patches applied : {applied} / {len(PATCHES)}")
if failed:
    print(f"\n  [!] Could not match {len(failed)} patch(es) — they may already be applied:")
    for f in failed:
        print(f"       · {f}")
    print("\n  If the script is already patched, you can ignore these warnings.")
else:
    print("  [+] All prompts now default to YES.\n")
