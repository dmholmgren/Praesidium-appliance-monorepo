"""
modules/ediscovery/jobs/folder_decompose.py
============================================
Automatic folder decomposition for eDiscovery ingest.

When a user selects a folder as a source, this module scans it and
determines whether it should be ingested as-is or decomposed into
multiple child collections. This is transparent to the user - they
see one parent collection that tracks aggregate progress.

Decomposition rules:
  1. Folder contains multiple archives -> one child per archive
  2. Folder contains subfolders with files -> one child per subfolder
  3. Loose files at the top level -> bundled into one child
  4. Single archive or flat production dir -> no decomposition, ingest as-is

Patent Pending - 64/020,027
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

ARCHIVE_EXTENSIONS = {".zip", ".tar", ".tar.gz", ".tgz", ".7z", ".rar", ".pst"}
METADATA_EXTENSIONS = {".dat", ".opt", ".lfp", ".log"}

# Thresholds
MIN_ARCHIVES_TO_DECOMPOSE = 2
MIN_TOTAL_SIZE_BYTES = 500 * 1024 * 1024  # 500MB


def scan_folder_source(dir_path: str) -> dict:
    """
    Pre-scan a directory source and classify its contents.
    Returns scan result with should_decompose flag and itemised contents.
    """
    archives = []
    subfolders = []
    loose_files = []
    total_file_count = 0
    total_size = 0

    if not os.path.isdir(dir_path):
        return {"should_decompose": False, "reason": "not_a_directory",
                "archives": [], "subfolders": [], "loose_files": [],
                "total_file_count": 0, "total_size_bytes": 0}

    try:
        entries = sorted(os.scandir(dir_path), key=lambda e: e.name.lower())
    except PermissionError:
        return {"should_decompose": False, "reason": "permission_denied",
                "archives": [], "subfolders": [], "loose_files": [],
                "total_file_count": 0, "total_size_bytes": 0}

    for entry in entries:
        if entry.name.startswith('.') or entry.name.startswith('@'):
            continue

        if entry.is_file(follow_symlinks=True):
            try:
                sz = entry.stat(follow_symlinks=True).st_size
            except OSError:
                sz = 0
            ext = Path(entry.name).suffix.lower()
            total_file_count += 1
            total_size += sz
            if ext in ARCHIVE_EXTENSIONS:
                archives.append({"name": entry.name, "path": entry.path, "size": sz})
            elif ext not in METADATA_EXTENSIONS:
                loose_files.append({"name": entry.name, "path": entry.path, "size": sz})

        elif entry.is_dir(follow_symlinks=True):
            sub_file_count = 0
            sub_size = 0
            try:
                for root, dirs, files in os.walk(entry.path):
                    dirs[:] = [d for d in dirs if not d.startswith('.') and not d.startswith('@')]
                    for f in files:
                        if not f.startswith('.'):
                            sub_file_count += 1
                            try:
                                sub_size += os.path.getsize(os.path.join(root, f))
                            except OSError:
                                pass
            except PermissionError:
                pass
            total_file_count += sub_file_count
            total_size += sub_size
            if sub_file_count > 0:
                subfolders.append({"name": entry.name, "path": entry.path,
                                   "file_count": sub_file_count, "size": sub_size})

    # Decision logic
    should_decompose = False
    reason = "single_source"

    if len(archives) >= MIN_ARCHIVES_TO_DECOMPOSE:
        should_decompose = True
        reason = f"multiple_archives ({len(archives)})"
    elif len(archives) >= 1 and len(subfolders) >= 1:
        should_decompose = True
        reason = f"mixed_content ({len(archives)} archives + {len(subfolders)} subfolders)"
    elif len(subfolders) >= 2 and total_size >= MIN_TOTAL_SIZE_BYTES:
        should_decompose = True
        reason = f"multiple_subfolders ({len(subfolders)}, {total_size / (1024**3):.1f}GB)"
    elif len(archives) == 1 and len(subfolders) == 0 and len(loose_files) <= 5:
        reason = "single_archive"
    elif len(subfolders) == 0 and len(archives) == 0:
        reason = "flat_files"

    return {
        "should_decompose": should_decompose, "reason": reason,
        "archives": archives, "subfolders": subfolders, "loose_files": loose_files,
        "total_file_count": total_file_count, "total_size_bytes": total_size,
    }


def build_child_specs(scan: dict, parent_name: str) -> list[dict]:
    """Build child collection specs from scan results."""
    children = []
    for arc in scan["archives"]:
        children.append({
            "name": f"{parent_name} \u2014 {Path(arc['name']).stem}",
            "source_path": arc["path"],
            "source_desc": f"Archive: {arc['name']} ({_fmt_size(arc['size'])})",
        })
    for sub in scan["subfolders"]:
        children.append({
            "name": f"{parent_name} \u2014 {sub['name']}",
            "source_path": sub["path"],
            "source_desc": f"Folder: {sub['name']} ({sub['file_count']} files, {_fmt_size(sub['size'])})",
        })
    if scan["loose_files"]:
        total_loose = sum(f["size"] for f in scan["loose_files"])
        paths = ",".join(f["path"] for f in scan["loose_files"])
        children.append({
            "name": f"{parent_name} \u2014 Loose Files",
            "source_path": paths,
            "source_desc": f"Loose files: {len(scan['loose_files'])} files ({_fmt_size(total_loose)})",
        })
    return children


def _fmt_size(sz: int) -> str:
    if sz < 1024: return f"{sz} B"
    elif sz < 1048576: return f"{sz / 1024:.1f} KB"
    elif sz < 1073741824: return f"{sz / 1048576:.1f} MB"
    else: return f"{sz / 1073741824:.1f} GB"
