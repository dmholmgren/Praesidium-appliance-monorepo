"""
split_zip_reassemble.py — Split Relativity ZIP support for eDiscovery ingest.

Handles multi-part Relativity/WinZip exports. Two layouts are recognized:

  A. Relativity byte-split:  Production.001.zip, .002.zip, .003.zip
  B. WinZip split:           Production.z01, .z02, ..., Production.zip
                             (the .zip is the LAST part, holding the EOCD)

Both are split at fixed byte boundaries; reassembly is binary concatenation in
segment order. (True multi-disk *spanned* archives carrying a PK\\x07\\x08
spanning marker are not produced by these vendor exports and are out of scope.)

Patent Pending — 64/020,027
"""

import logging
import os
import re
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

SPLIT_ZIP_PATTERN = re.compile(r'^(.+)\.(\d{3})\.zip$', re.IGNORECASE)   # name.001.zip
WINZIP_SEG_PATTERN = re.compile(r'^(.+)\.z(\d{2,})$', re.IGNORECASE)     # name.z01 ... name.z1008


def _winzip_split(source_paths: list[str]) -> tuple[bool, str, list[str]]:
    """WinZip-style split set: name.z01, name.z02, ..., name.zip.

    The .zip is the final segment (holds the end-of-central-directory);
    reassembly order is z01..zNN then .zip.
    """
    zips = [p for p in source_paths if p.lower().endswith(".zip")]
    segs = []
    for p in source_paths:
        m = WINZIP_SEG_PATTERN.match(os.path.basename(p))
        if m:
            segs.append((int(m.group(2)), m.group(1), p))
    # exactly one .zip terminator, at least one .zNN, and nothing else
    if len(zips) != 1 or not segs or (len(zips) + len(segs) != len(source_paths)):
        return False, "", []
    base = os.path.splitext(os.path.basename(zips[0]))[0]
    if any(s[1] != base for s in segs):
        return False, "", []
    ordered = [p for _, _, p in sorted(segs, key=lambda x: x[0])] + [zips[0]]
    return True, base, ordered


def detect_split_zip(source_paths: list[str]) -> tuple[bool, str, list[str]]:
    """
    Detect whether a list of source paths represents a split ZIP set.

    Returns:
        (is_split, base_name, sorted_segments)
    """
    if len(source_paths) < 2:
        return False, "", []

    # Format A — Relativity byte-split: name.001.zip / name.002.zip / ...
    bases = {}
    matched_all = True
    for p in source_paths:
        m = SPLIT_ZIP_PATTERN.match(os.path.basename(p))
        if not m:
            matched_all = False
            break
        bases.setdefault(m.group(1), []).append((int(m.group(2)), p))
    if matched_all and len(bases) == 1:
        base_name = list(bases.keys())[0]
        segments = sorted(bases[base_name], key=lambda x: x[0])
        return True, base_name, [s[1] for s in segments]

    # Format B — WinZip split: name.z01 ... name.zip
    return _winzip_split(source_paths)


def reassemble_split_zip(
    sorted_segments: list[str],
    base_name: str,
    output_dir: str,
    jlog=None,
    log_progress_fn=None,
    tenant_id: str = "",
    collection_id: str = "",
) -> str:
    """
    Concatenate split ZIP segments into a single ZIP file.

    Uses 64MB chunked streaming — handles 100GB+ segments without
    requiring the file to fit in RAM.

    Returns absolute path to the reassembled ZIP.
    Raises RuntimeError on failure.
    """
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, f"{base_name}.zip")

    # Skip if already reassembled and size matches
    if os.path.exists(output_path):
        expected_size = sum(os.path.getsize(s) for s in sorted_segments)
        actual_size = os.path.getsize(output_path)
        if abs(actual_size - expected_size) < 1024:
            msg = f"Split ZIP already reassembled: {output_path} ({actual_size:,} bytes)"
            logger.info(msg)
            if jlog:
                jlog.info(msg)
            return output_path
        else:
            logger.info(
                "Existing reassembled ZIP size mismatch (%d vs expected %d), rebuilding",
                actual_size, expected_size,
            )
            os.unlink(output_path)

    total_size = sum(os.path.getsize(s) for s in sorted_segments)
    msg = (
        f"Reassembling split ZIP: {len(sorted_segments)} segments, "
        f"{total_size / (1024**3):.1f} GB total -> {output_path}"
    )
    logger.info(msg)
    if jlog:
        jlog.info(msg)
    if log_progress_fn:
        log_progress_fn(tenant_id, collection_id, msg)

    try:
        with open(output_path, 'wb') as outf:
            for i, seg_path in enumerate(sorted_segments):
                seg_size = os.path.getsize(seg_path)
                seg_msg = (
                    f"  Concatenating segment {i+1}/{len(sorted_segments)}: "
                    f"{os.path.basename(seg_path)} ({seg_size / (1024**3):.1f} GB)"
                )
                logger.info(seg_msg)
                if jlog:
                    jlog.info(seg_msg)
                if log_progress_fn:
                    log_progress_fn(tenant_id, collection_id, seg_msg)

                with open(seg_path, 'rb') as inf:
                    # zero-copy kernel path; 64MB userspace chunks as fallback
                    outf.flush()
                    try:
                        off = 0
                        while off < seg_size:
                            sent = os.sendfile(outf.fileno(), inf.fileno(),
                                               off, 1 << 30)
                            if sent == 0:
                                break
                            off += sent
                        if off < seg_size:
                            raise OSError("sendfile short transfer")
                    except OSError:
                        inf.seek(0)
                        while True:
                            chunk = inf.read(64 * 1024 * 1024)
                            if not chunk:
                                break
                            outf.write(chunk)

    except Exception as e:
        if os.path.exists(output_path):
            os.unlink(output_path)
        raise RuntimeError(f"Split ZIP reassembly failed: {e}") from e

    # Verify valid ZIP
    if not zipfile.is_zipfile(output_path):
        raise RuntimeError(
            f"Reassembled file is not a valid ZIP: {output_path} "
            f"({os.path.getsize(output_path):,} bytes)"
        )

    final_size = os.path.getsize(output_path)
    msg = f"Split ZIP reassembled successfully: {final_size / (1024**3):.1f} GB"
    logger.info(msg)
    if jlog:
        jlog.info(msg)
    if log_progress_fn:
        log_progress_fn(tenant_id, collection_id, msg)

    return output_path
