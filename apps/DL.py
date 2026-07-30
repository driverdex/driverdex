#!/usr/bin/env python3
"""
DriverDex Downloader & Extractor  —  plain-blob edition
====================================================
Archives are stored as regular Git blobs (not LFS).
raw.githubusercontent.com serves them as binary directly —
no batch API, no pointer files, no fallback chains.

Download strategy per part:
  1. GET raw.githubusercontent.com URL  (plain binary)
  2. Verify SHA-256
  3. Retry up to RETRIES times with exponential back-off
"""
from __future__ import annotations

import errno
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ── optional rich UI ──────────────────────────────────────────────────────────
def _ensure_package(pkg: str, import_name: Optional[str] = None) -> None:
    name = import_name or pkg
    try:
        __import__(name)
    except Exception:
        print(f"[!] Installing missing dependency: {pkg}", flush=True)
        subprocess.run([sys.executable, "-m", "pip", "install", pkg, "-q"], check=False)

_ensure_package("rich")

try:
    import py7zr          # type: ignore
except Exception:
    py7zr = None          # type: ignore

try:
    import multivolumefile  # type: ignore
except Exception:
    multivolumefile = None  # type: ignore

from rich.console  import Console
from rich.panel    import Panel
from rich.progress import (
    BarColumn, DownloadColumn, Progress, SpinnerColumn,
    TaskProgressColumn, TextColumn, TimeRemainingColumn, TransferSpeedColumn,
)
from rich.prompt   import Prompt
from rich.table    import Table
from rich.text     import Text

# ── configuration ─────────────────────────────────────────────────────────────
REPO_OWNER   = "rhshourav"
REPO_NAME    = "driverdex"
BRANCH       = "main"

BASE_RAW_URL = f"https://raw.githubusercontent.com/{REPO_OWNER}/{REPO_NAME}/{BRANCH}"

DOWNLOAD_DIR  = Path("DriverDex_Downloads")
EXTRACT_DIR   = Path("DriverDex_Extracted")
TIMEOUT       = 60
RETRIES       = 5
BACKOFF_BASE  = 1.5

C = Console(highlight=False)


# ── helpers ───────────────────────────────────────────────────────────────────
def _safe_name(value: str, fallback: str = "item") -> str:
    value = (value or "").strip()
    value = re.sub(r"[^\w.\-]+", "_", value, flags=re.UNICODE)
    value = value.strip("._-")
    return value or fallback


def _human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if num < 1024.0:
            return f"{num:.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def _divider(title: str = "") -> None:
    if title:
        C.print(Panel.fit(title, border_style="bright_cyan"))
    else:
        C.rule(style="bright_cyan")


def _ok(msg: str)   -> None: C.print(f"[bold bright_green]✓[/bold bright_green] {msg}")
def _warn(msg: str) -> None: C.print(f"[bold yellow]⚠[/bold yellow] {msg}")
def _err(msg: str)  -> None: C.print(f"[bold bright_red]✗[/bold bright_red] {msg}")
def _info(msg: str) -> None: C.print(f"[bold bright_cyan]◈[/bold bright_cyan] {msg}")


def _retry_sleep(attempt: int) -> None:
    time.sleep(min(BACKOFF_BASE * (2 ** (attempt - 1)), 30.0))


def _ensure_dirs() -> None:
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    EXTRACT_DIR.mkdir(parents=True, exist_ok=True)


def _request(
    url: str,
    *,
    method:  str = "GET",
    data:    Optional[bytes] = None,
    headers: Optional[Dict[str, str]] = None,
    timeout: int = TIMEOUT,
) -> urllib.request.Request:
    req_headers = {
        "User-Agent": f"DriverDex-Downloader/3.0 (+https://github.com/{REPO_OWNER}/{REPO_NAME})",
    }
    if headers:
        req_headers.update(headers)
    return urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())


# ── manifest fetching ─────────────────────────────────────────────────────────
def _fetch_json(url: str) -> Optional[Dict]:
    last_exc: Optional[Exception] = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = _request(url)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        except Exception as exc:
            last_exc = exc
            if attempt < RETRIES:
                _warn(f"Request failed ({attempt}/{RETRIES}): {exc} — retrying…")
                _retry_sleep(attempt)
    _err(f"Could not fetch JSON: {url}")
    if last_exc:
        _warn(str(last_exc))
    return None


# ── streaming download ────────────────────────────────────────────────────────
def _download_stream(
    url:             str,
    dest:            Path,
    *,
    size_hint:       int = 0,
    headers:         Optional[Dict[str, str]] = None,
    expected_sha256: str = "",
) -> bool:
    """
    Stream *url* to *dest*, verifying SHA-256 on success.
    Works for plain Git blobs served by raw.githubusercontent.com.
    Retries up to RETRIES times with exponential back-off.
    """
    temp = dest.with_name(dest.name + ".part")
    if temp.exists():
        try:
            temp.unlink()
        except Exception:
            pass

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    req_headers: Dict[str, str] = headers.copy() if headers else {}
    if token and "Authorization" not in req_headers:
        req_headers["Authorization"] = f"Bearer {token}"

    last_exc: Optional[Exception] = None
    for attempt in range(1, RETRIES + 1):
        downloaded = 0
        sha_h = hashlib.sha256() if expected_sha256 else None
        try:
            req = _request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp, open(temp, "wb") as out:
                total_hdr  = resp.headers.get("Content-Length")
                total_bytes = int(total_hdr) if total_hdr and total_hdr.isdigit() else size_hint
                C.print(
                    f"    [bold bright_cyan]↓[/bold bright_cyan] "
                    f"{dest.name}  [dim]({_human_size(total_bytes or size_hint)})[/dim]"
                )
                while True:
                    chunk = resp.read(1024 * 128)
                    if not chunk:
                        break
                    out.write(chunk)
                    downloaded += len(chunk)
                    if sha_h:
                        sha_h.update(chunk)
                    if total_bytes:
                        pct = min(100, int(downloaded * 100 / max(total_bytes, 1)))
                        C.print(f"\r       [cyan]Progress[/cyan]: {pct:3d}%   ", end="")

            # Quick sanity check: did we get an LFS pointer or HTML error page?
            preview = b""
            try:
                with open(temp, "rb") as fh:
                    preview = fh.read(512)
            except Exception:
                pass
            preview_txt = preview.decode("utf-8", errors="ignore")
            if "git-lfs.github.com/spec/v1" in preview_txt:
                raise IOError(
                    "Received an LFS pointer instead of binary data. "
                    "This file may still be tracked by LFS — re-run the builder "
                    "with blob-upload mode to fix the repository."
                )
            if preview_txt.lstrip().startswith(("<!DOCTYPE", "<html", "{")):
                try:
                    payload = json.loads(preview_txt)
                    msg = payload.get("message", "")
                except Exception:
                    msg = ""
                if msg or preview_txt.lstrip().startswith(("<!DOCTYPE", "<html")):
                    raise IOError(f"Server returned an error page: {msg or preview_txt[:80]!r}")

            if expected_sha256 and sha_h:
                digest = sha_h.hexdigest()
                if digest.lower() != expected_sha256.lower():
                    raise IOError(
                        f"SHA-256 mismatch: expected {expected_sha256[:12]}…, got {digest[:12]}…"
                    )
            elif size_hint and downloaded != size_hint:
                _warn(f"Size mismatch: expected {size_hint}, got {downloaded} — accepting anyway")

            try:
                temp.replace(dest)
            except Exception:
                shutil.move(str(temp), str(dest))

            C.print()
            return True

        except Exception as exc:
            last_exc = exc
            try:
                if temp.exists():
                    temp.unlink()
            except Exception:
                pass
            if attempt < RETRIES:
                _warn(f"Download failed ({attempt}/{RETRIES}): {exc} — retrying…")
                _retry_sleep(attempt)
            else:
                _err(f"Download failed after {RETRIES} attempts: {exc}")

    if last_exc:
        _warn(f"Last error: {last_exc}")
    return False


# ── extraction ────────────────────────────────────────────────────────────────
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_zip(path: Path) -> bool:
    return path.suffix.lower() == ".zip" or zipfile.is_zipfile(path)


def _is_7z_volume(name: str) -> bool:
    n = name.lower()
    return bool(
        re.search(r"\.7z\.\d+$", n)
        or re.search(r"\.7z\.\d{4}$", n)
        or n.endswith(".7z")
    )


def _extract_zip(path: Path, dest: Path) -> None:
    with zipfile.ZipFile(path, "r") as zf:
        zf.extractall(dest)


def _extract_7z(first_part: Path, dest: Path) -> None:
    if py7zr is None or multivolumefile is None:
        raise RuntimeError("py7zr and multivolumefile are required for 7z extraction")
    base = first_part
    name = first_part.name.lower()
    m = re.match(r"^(.*\.7z)\.\d{4}$", name)
    if m:
        base = first_part.with_name(m.group(1))
    elif re.match(r"^(.*\.7z)\.\d+$", name):
        base = first_part.with_name(name.rsplit(".", 1)[0])

    with multivolumefile.open(str(base), mode="rb") as target_archive:
        with py7zr.SevenZipFile(target_archive, mode="r") as archive:
            archive.extractall(path=str(dest))


def _extract_payload(parts: Sequence[Path], dest_root: Path, label: str) -> Path:
    out = dest_root / _safe_name(label, "extracted")
    out.mkdir(parents=True, exist_ok=True)

    ordered = sorted(parts, key=lambda p: (p.name.lower(), p.stat().st_size))
    if not ordered:
        raise ValueError("no downloaded files to extract")

    zip_parts = [p for p in ordered if _is_zip(p)]
    if zip_parts:
        if len(zip_parts) == 1 and not any(".part" in p.name.lower() for p in zip_parts):
            _info(f"Extracting ZIP archive: {zip_parts[0].name}")
            _extract_zip(zip_parts[0], out)
        else:
            _info(f"Extracting {len(zip_parts)} ZIP volume(s)…")
            for z in zip_parts:
                _extract_zip(z, out)
        return out

    seven_parts = [p for p in ordered if _is_7z_volume(p.name)]
    if seven_parts:
        _info(f"Extracting 7z package from {seven_parts[0].name}")
        _extract_7z(seven_parts[0], out)
        return out

    # Fallback: copy files as-is.
    for p in ordered:
        shutil.copy2(p, out / p.name)
    return out


# ── manifest helpers ──────────────────────────────────────────────────────────
def _fetch_manifest(url: str) -> Optional[Dict]:
    return _fetch_json(url)


def _manifest_entries(manifest: Dict) -> List[Dict]:
    for key in ("drivers", "installers", "items"):
        val = manifest.get(key)
        if isinstance(val, list) and val:
            return val
    return []


def _manifest_title(manifest_name: str) -> str:
    # manifest_name may be a repo-relative path like "manifests/Audio.manifest.json";
    # show only the human-friendly category stem.
    base = manifest_name.rsplit("/", 1)[-1]
    return base.replace(".manifest.json", "").replace(".manifest", "")


def _detect_item_kind(item: Dict) -> str:
    if "provider" in item or "hwids" in item:
        return "driver"
    if "company" in item or "installer_type" in item or "exe_filename" in item:
        return "installer"
    return "package"


def _item_label(item: Dict) -> str:
    kind = _detect_item_kind(item)
    if kind == "driver":
        provider = item.get("provider") or item.get("company") or "Unknown"
        category = item.get("category") or item.get("type") or "Misc"
        version  = item.get("version") or "N/A"
        return f"{provider} - {category} (v{version})"
    if kind == "installer":
        name    = item.get("filename") or item.get("exe_filename") or "Unknown"
        company = item.get("company") or "Misc"
        itype   = item.get("installer_type") or "N/A"
        return f"{name} - {company} ({itype})"
    return item.get("filename") or item.get("id") or "Unknown"


def _get_parts(item: Dict) -> List[Dict]:
    parts = item.get("parts")
    if isinstance(parts, list) and parts:
        return parts
    # Backward-compat: single URL entry.
    if item.get("zip"):
        return [{
            "part_num"  : 1,
            "filename"  : Path(urllib.parse.urlparse(item["zip"]).path).name
                          or f"{item.get('id', 'item')}.zip",
            "size_bytes": int(item.get("size_bytes") or 0),
            "sha256"    : item.get("sha256", ""),
            "url"       : item["zip"],
        }]
    return []


# ── download ──────────────────────────────────────────────────────────────────
def _download_part(part: Dict, item_dir: Path) -> Optional[Path]:
    """
    Download one archive part from its raw.githubusercontent.com URL.

    The URL points to a plain Git blob — no LFS resolution needed.
    SHA-256 is verified if present in the manifest.
    """
    filename     = part.get("filename") or part.get("name") or "part.bin"
    dest         = item_dir / filename
    expected_sha = (part.get("sha256") or "").strip()
    size         = int(part.get("size_bytes") or part.get("lfs_size") or 0)
    raw_url      = (part.get("url") or part.get("download_url") or "").strip()

    if not raw_url:
        _err(f"No URL in manifest for {filename}.")
        return None

    # Skip re-download if file already exists and SHA matches.
    if dest.exists() and expected_sha:
        if _sha256(dest).lower() == expected_sha.lower():
            _ok(f"Already downloaded (SHA ok): {filename}")
            return dest
        else:
            _warn(f"Cached file failed SHA check, re-downloading: {filename}")
            try:
                dest.unlink()
            except Exception:
                pass

    _info(f"Downloading part: {filename}")
    if _download_stream(raw_url, dest, size_hint=size, expected_sha256=expected_sha):
        _ok(f"Downloaded: {filename}")
        return dest

    _err(
        f"Could not download {filename}.\n"
        f"  URL : {raw_url}\n"
        f"  Tip : If you see an LFS pointer error, the builder needs to re-upload\n"
        f"        this file using blob mode.  Set GITHUB_TOKEN and re-run the builder."
    )
    return None


# ── UI ────────────────────────────────────────────────────────────────────────
def _show_banner() -> None:
    os.system("cls" if os.name == "nt" else "clear")
    art = Text()
    art.append("  ██╗     ██████╗  ██████╗\n",  style="bold bright_cyan")
    art.append("  ██║     ██╔══██╗██╔════╝\n",  style="bold bright_cyan")
    art.append("  ██║     ██║  ██║██║     \n",  style="bold bright_cyan")
    art.append("  ██║     ██║  ██║██║     \n",  style="bold bright_cyan")
    art.append("  ███████╗██████╔╝╚██████╗\n",  style="bold bright_cyan")
    art.append("  ╚══════╝╚═════╝  ╚═════╝\n",  style="bold bright_cyan")

    panel = Panel(
        art + Text("\n  Downloader & Extractor\n", style="bold white"),
        border_style="bright_cyan",
        title="[bold bright_cyan]  DriverDex  [/bold bright_cyan]",
        subtitle=f"[bright_cyan]github.com/{REPO_OWNER}/{REPO_NAME}[/bright_cyan]",
        padding=(0, 4),
    )
    C.print(panel)


def _show_categories(manifest_names: Sequence[str]) -> None:
    table = Table(title="Available Categories", title_style="bold bright_cyan", box=None)
    table.add_column("#", style="bold bright_cyan", justify="right")
    table.add_column("Category", style="bold white")
    for i, name in enumerate(manifest_names):
        table.add_row(str(i), _manifest_title(name))
    C.print(table)


def _show_items(items: Sequence[Dict]) -> None:
    table = Table(title="Available Items", title_style="bold bright_cyan")
    table.add_column("#",     style="bold bright_cyan", justify="right", no_wrap=True)
    table.add_column("Item",  style="white")
    table.add_column("Type",  style="bold yellow",  no_wrap=True)
    table.add_column("Parts", style="bold green",   justify="right", no_wrap=True)
    for i, item in enumerate(items):
        parts = _get_parts(item)
        table.add_row(str(i), _item_label(item), _detect_item_kind(item), str(len(parts)))
    C.print(table)


def _choose_index(prompt: str, limit: int) -> Optional[int]:
    while True:
        try:
            raw = Prompt.ask(prompt, default="q").strip().lower()
        except (KeyboardInterrupt, EOFError):
            return None
        if raw in {"q", "quit", "exit"}:
            return None
        try:
            val = int(raw)
            if 0 <= val < limit:
                return val
        except ValueError:
            pass
        _warn(f"Choose a number from 0 to {max(0, limit - 1)}, or q to quit.")


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    _show_banner()
    _ensure_dirs()

    _info("Fetching repository index…")
    index = _fetch_manifest(f"{BASE_RAW_URL}/driverdex_index.json")
    if not index:
        return

    manifest_shards = index.get("manifest_shards", [])
    manifests = [
        m["filename"]
        for m in manifest_shards
        if isinstance(m, dict) and m.get("filename")
    ]
    # Installer manifest now lives under manifests/ alongside the category manifests.
    if "manifests/installers.manifest.json" not in manifests:
        manifests.append("manifests/installers.manifest.json")

    if not manifests:
        _err("No manifests were found in the repository index.")
        return

    _divider("Choose a category")
    _show_categories(manifests)
    cat_idx = _choose_index("Select a category number", len(manifests))
    if cat_idx is None:
        _warn("Cancelled.")
        return

    manifest_name = manifests[cat_idx]
    _info(f"Fetching manifest: {manifest_name}")
    manifest = _fetch_manifest(f"{BASE_RAW_URL}/{manifest_name}")
    if not manifest:
        return

    items = _manifest_entries(manifest)
    if not items:
        _warn("No items found in this manifest.")
        return

    _divider(f"Items in {_manifest_title(manifest_name)}")
    _show_items(items)
    item_idx = _choose_index("Select an item number to download", len(items))
    if item_idx is None:
        _warn("Cancelled.")
        return

    item  = items[item_idx]
    parts = _get_parts(item)
    if not parts:
        _err("This item has no downloadable parts in the manifest.")
        return

    item_name     = _safe_name(item.get("id") or item.get("filename") or item.get("provider") or "item")
    category_name = _safe_name(_manifest_title(manifest_name), "category")

    item_download_dir = DOWNLOAD_DIR / category_name / item_name
    item_extract_dir  = EXTRACT_DIR  / category_name / item_name
    item_download_dir.mkdir(parents=True, exist_ok=True)
    item_extract_dir.parent.mkdir(parents=True, exist_ok=True)

    _divider("Download")
    _info(f"Preparing {len(parts)} part(s) for: {item_name}")

    downloaded: List[Path] = []
    progress = Progress(
        SpinnerColumn(style="bold bright_cyan"),
        TextColumn("[bold bright_cyan]{task.description}[/bold bright_cyan]"),
        BarColumn(bar_width=None, complete_style="bold bright_cyan",
                  finished_style="bold bright_green"),
        TaskProgressColumn(),
        DownloadColumn(binary_units=True),
        TransferSpeedColumn(),
        TimeRemainingColumn(compact=True, elapsed_when_finished=True),
        console=C, transient=False, expand=True,
    )

    with progress:
        total = sum(int(p.get("size_bytes") or 0) for p in parts) or None
        task  = progress.add_task("Downloading", total=total)
        for part in sorted(parts, key=lambda p: int(p.get("part_num") or 0)):
            p = _download_part(part, item_download_dir)
            if not p:
                _err(f"Failed on part: {part.get('filename', 'unknown')}")
                return
            downloaded.append(p)
            progress.advance(task, int(part.get("size_bytes") or 0) or 1)

    _divider("Extract")
    try:
        extracted = _extract_payload(downloaded, item_extract_dir, item_name)
        _ok("Extraction complete.")
        _ok(f"Ready: {extracted}")
    except Exception as exc:
        _err(f"Extraction failed: {exc}")
        _warn("The files are still saved in the download folder.")

    C.print()
    C.print(Panel.fit(
        f"[bold bright_green]Done[/bold bright_green]\n"
        f"[white]Downloads :[/white] {item_download_dir}\n"
        f"[white]Extracted :[/white] {item_extract_dir}",
        border_style="bright_green",
        title="[bold bright_green]  DriverDex Downloader & Extractor  [/bold bright_green]",
        padding=(0, 2),
    ))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        C.print()
        _warn("Interrupted by user.")
        sys.exit(130)
