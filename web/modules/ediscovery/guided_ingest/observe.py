"""
modules/ediscovery/guided_ingest/observe.py

OBSERVE + deterministic classify for guided ingestion (read-only, no DB, no LLM).

Walks exactly what the user proposes to ingest (a fresh scoped walk -- NOT the
stale global file_inventory), inspects archives/PSTs cheaply without extracting,
sanity-checks for junk and zip-vs-loose duplication, and proposes a skeleton of
LOGICAL ingestion units:

  PST            -> one collection per custodian (years grouped; corrupt/empty
                    PSTs flagged, not silently ingested)
  load-file set  -> its own load-file production
  zip w/ loadfile-> its own load-file production
  multi-part zip -> reassemble -> load-file production
  loose files    -> one collection per selected root (small non-loadfile zips
                    folded in, not one-collection-each)
  re-packaging zip / junk / corrupt -> flagged, never ingested blind

For loose (client-files) roots it also runs deterministic family rules splitting
files into DMS-bound business records vs. a RESIDUE the caller escalates to a
frontier model. Every file is accounted for (placed or flagged) -- the "account"
discipline. Ported from the validated /tmp/ii_observe_v5 + cf_classify_det
prototypes (Kay + Marcus matters).
"""
import os
import re
from collections import defaultdict

LOADFILE_EXTS = {".dat", ".opt", ".lfp", ".dii"}
PST_EXTS = {".pst", ".ost"}
ZIP_EXTS = {".zip"}
TAR_PLAIN = {".tar"}
TAR_COMPRESSED = {".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tbz2"}
OTHER_ARCHIVE = {".7z", ".rar", ".gz"}
JUNK_NAMES = {"thumbs.db", ".ds_store", "desktop.ini", "ingestion.log"}
JUNK_PREFIXES = ("~$",)
JUNK_SUFFIXES = (".tmp",)
EMAIL_EXTS = {".msg", ".eml"}
SHEET_EXTS = {".xls", ".xlsx", ".csv"}

# A non-loadfile zip at/under this entry count is treated as loose content packed
# into an archive (e.g. single-invoice Factura_*.zip) and folded into its root's
# loose collection, rather than spawning its own collection.
SMALL_ZIP_ENTRIES = 25

DATE_TOKEN = re.compile(r"\b(\d{1,2}[._-]\d{1,2}[._-]\d{2,4})\b")
TIME_TOKEN = re.compile(r"\b(\d{3,4}\s?[ap]m)\b", re.I)
YEAR_TOKEN = re.compile(r"\b(19|20)\d{2}\b")

# Deterministic client-files family rules. (rule_id, regex, bucket, reason).
# First match wins; bucket is "dms" or "promote" (email). Anything unmatched is
# RESIDUE -> escalate to frontier.
FAMILY_RULES = [
    ("email", re.compile(r"\.(msg|eml)$", re.I), "promote",
     "Mail message (.msg/.eml) -> communication; promote to eDiscovery."),
    ("cfdi_invoice", re.compile(r"_A_\d+_\d{10,}\.(xml|pdf)$", re.I), "dms",
     "CFDI tax e-invoice (RFC_A_folio_seal) -> business record -> DMS."),
    ("cfdi_rfc", re.compile(r"^[A-Z&N]{3,4}\d{6}[A-Z0-9]{2,3}_A_\d+", re.I), "dms",
     "CFDI e-invoice (issuer-RFC prefix) -> business record -> DMS."),
    ("factura_zip", re.compile(r"^Factura_.*\.zip$", re.I), "dms",
     "Invoice archive (Factura_*.zip) -> business record -> DMS."),
    ("nota_credito", re.compile(r"NOTA\s+DE\s+CREDITO", re.I), "dms",
     "Credit note -> accounting record -> DMS."),
    ("pre_factura", re.compile(r"PRE[-\s]?FACTURA", re.I), "dms",
     "Pre-invoice -> business record -> DMS."),
    ("packing_slip", re.compile(r"(-PS\.(pdf|xlsx)$|^PS[\s\d])", re.I), "dms",
     "Packing slip / price sheet -> business record -> DMS."),
    ("purchase_order", re.compile(r"(PO\s*\d|FOLIO)", re.I), "dms",
     "Purchase-order document -> business record -> DMS."),
]


def lower_ext(name):
    n = name.lower()
    for e in TAR_COMPRESSED:
        if n.endswith(e):
            return e
    _, ext = os.path.splitext(n)
    return ext


SPLIT_RE = (
    re.compile(r"^(?P<base>.+?)\.(?P<part>\d{3})\.zip$", re.I),
    re.compile(r"^(?P<base>.+?\.zip)\.(?P<part>\d{3})$", re.I),
    re.compile(r"^(?P<base>.+?)\.z(?P<part>\d{2})$", re.I),
)


def split_part(name):
    for rx in SPLIT_RE:
        m = rx.match(name)
        if m:
            return m.group("base"), m.group("part")
    return None


def is_junk(name):
    n = name.lower()
    if n in JUNK_NAMES:
        return True
    if any(n.startswith(p) for p in JUNK_PREFIXES):
        return True
    if any(n.endswith(s) for s in JUNK_SUFFIXES):
        return True
    return False


def classify(name):
    if is_junk(name):
        return "junk"
    ext = lower_ext(name)
    if ext in PST_EXTS:
        return "pst"
    if ext in ZIP_EXTS:
        return "zip"
    if ext in TAR_PLAIN:
        return "tar"
    if ext in TAR_COMPRESSED:
        return "tar_compressed"
    if ext in OTHER_ARCHIVE:
        return "archive_opaque"
    if ext in LOADFILE_EXTS:
        return "loadfile"
    return "loose"


def custodian_from_filename(stem):
    s = DATE_TOKEN.sub(" ", stem)
    s = TIME_TOKEN.sub(" ", s)
    s = re.sub(r"[._\-]+", " ", s)
    year = None
    m = YEAR_TOKEN.search(s)
    if m:
        year = m.group(0)
    s = YEAR_TOKEN.sub(" ", s)
    s = re.sub(r"^[A-Za-z]{2,5}\s?discovery\s?", " ", s, flags=re.I)
    s = re.sub(r"\b(discovery|export|backup|mailbox|archive|pst|ost)\b", " ",
               s, flags=re.I)
    s = re.sub(r"\s+", " ", s).strip(" -_")
    return (s or stem).strip(), year


_FOLDERY = ("folder", "search", "spam", "junk", "inbox", "outbox", "sent",
            "draft", "deleted", "archive", "calendar", "contact", "task",
            "note", "journal", "rss", "sync issue", "conversation",
            "top of", "ipm", "mailbox", "personal folders")


def _looks_like_owner(name):
    low = name.strip().lower()
    if not low or len(low) < 3:
        return False
    if any(tok in low for tok in _FOLDERY):
        return False
    return True


def _store_owner(pff_file):
    for getter in ("get_message_store", "message_store"):
        try:
            store = getattr(pff_file, getter)
            store = store() if callable(store) else store
            for attr in ("get_display_name", "display_name"):
                v = getattr(store, attr, None)
                v = v() if callable(v) else v
                if v:
                    return str(v)
        except Exception:
            continue
    return None


def inspect_pst(path):
    out = {"custodian": None, "custodian_source": "filename",
           "messages": None, "folders": None, "year": None, "error": None}
    stem = os.path.splitext(os.path.basename(path))[0]
    cust, year = custodian_from_filename(stem)
    out["custodian"], out["year"] = cust, year
    try:
        import pypff
        f = pypff.file()
        f.open(path)
        root = f.get_root_folder()
        acc = {"msgs": 0, "folders": 0}

        def walk(folder, depth=0):
            acc["folders"] += 1
            try:
                acc["msgs"] += folder.get_number_of_sub_messages()
            except Exception:
                pass
            if depth < 8:
                try:
                    for i in range(folder.get_number_of_sub_folders()):
                        walk(folder.get_sub_folder(i), depth + 1)
                except Exception:
                    pass

        walk(root)
        out["messages"] = acc["msgs"]
        out["folders"] = acc["folders"]
        owner = _store_owner(f)
        if owner and _looks_like_owner(owner):
            out["custodian"] = owner
            out["custodian_source"] = "pst_owner"
        f.close()
    except Exception as e:
        out["error"] = "{}: {}".format(type(e).__name__, e)
    return out


def inspect_zip(path):
    import zipfile
    out = {"enumerated": False, "entries": 0, "uncompressed": 0,
           "has_loadfile": False, "loadfile_names": [], "entry_index": {},
           "error": None}
    try:
        with zipfile.ZipFile(path) as z:
            files = [i for i in z.infolist() if not i.is_dir()]
            out["enumerated"] = True
            out["entries"] = len(files)
            out["uncompressed"] = sum(i.file_size for i in files)
            for i in files:
                base = os.path.basename(i.filename)
                if not base:
                    continue
                if lower_ext(base) in LOADFILE_EXTS:
                    out["has_loadfile"] = True
                    out["loadfile_names"].append(base)
                key = (base.lower(), i.file_size)
                out["entry_index"][key] = out["entry_index"].get(key, 0) + 1
    except Exception as e:
        out["error"] = "{}: {}".format(type(e).__name__, e)
    return out


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return "{:.1f}{}".format(n, unit)
        n /= 1024
    return "{:.1f}PB".format(n)


def classify_loose_files(files):
    """files: list of (abspath, name, size). Returns
    {dms:[...], promote:[...], residue:[...]} of {name_pattern,count,example,reason}
    grouped by name pattern. Deterministic family rules only."""
    def pattern(name):
        stem, ext = os.path.splitext(name)
        p = re.sub(r"\d+", "#", stem)
        p = re.sub(r"\s+", " ", p).strip()
        return p[:48] + ext.lower()

    groups = {"dms": {}, "promote": {}, "residue": {}}
    for _fp, name, _sz in files:
        bucket, reason = "residue", "No deterministic rule matched -> escalate."
        for _rid, rx, bkt, rsn in FAMILY_RULES:
            if rx.search(name):
                bucket, reason = bkt, rsn
                break
        pat = pattern(name)
        g = groups[bucket].setdefault(
            pat, {"name_pattern": pat, "count": 0, "example": name, "reason": reason})
        g["count"] += 1
    return {k: sorted(v.values(), key=lambda d: -d["count"])
            for k, v in groups.items()}


def observe(paths):
    """Fresh scoped walk of `paths`. Returns the deterministic skeleton:
    {inventory, collections[], flagged[], loose_files_by_root}. No DB, no LLM."""
    roots = [os.path.abspath(p) for p in paths]
    files = []  # (abspath, root, name, kind, size)
    for root in roots:
        if os.path.isfile(root):
            files.append((root, os.path.dirname(root), os.path.basename(root),
                          classify(os.path.basename(root)),
                          os.path.getsize(root)))
            continue
        for dp, _dirs, fns in os.walk(root):
            for fn in fns:
                fp = os.path.join(dp, fn)
                try:
                    sz = os.path.getsize(fp)
                except OSError:
                    sz = 0
                files.append((fp, root, fn, classify(fn), sz))

    by_kind = defaultdict(list)
    ext_hist = defaultdict(int)
    total_bytes = 0
    for fp, root, fn, kind, sz in files:
        by_kind[kind].append((fp, root, fn, sz))
        ext_hist[lower_ext(fn) or "(none)"] += 1
        total_bytes += sz

    # split multi-part archives out of plain zips
    split_groups = defaultdict(list)
    singleton_zips = []
    for fp, root, fn, sz in by_kind["zip"]:
        sp = split_part(fn)
        if sp:
            base, part = sp
            split_groups[os.path.join(os.path.dirname(fp), base)].append((part, fp))
        else:
            singleton_zips.append((fp, root, fn, sz))

    pst_info = {fp: inspect_pst(fp) for (fp, *_ ) in by_kind["pst"]}
    zip_info = {fp: inspect_zip(fp) for (fp, *_ ) in singleton_zips}

    loose_index = defaultdict(int)
    for fp, root, fn, sz in (by_kind["loose"] + by_kind["pst"]
                             + by_kind["loadfile"]):
        loose_index[(fn.lower(), sz)] += 1

    collections = []
    flagged = []

    # PSTs -> one collection per custodian (years grouped; corrupt/empty flagged)
    cust_groups = defaultdict(list)
    for fp, *_ in by_kind["pst"]:
        info = pst_info[fp]
        if info["error"]:
            flagged.append({"path": fp, "type": "pst",
                            "reason": "PST failed to open (corrupt/truncated): "
                                      + info["error"].split(".")[0],
                            "suggested_action": "repair/re-export then re-inventory"})
            continue
        if (info["messages"] or 0) == 0:
            flagged.append({"path": fp, "type": "pst",
                            "reason": "PST opened but holds 0 messages (empty)",
                            "suggested_action": "exclude; confirm export complete"})
            continue
        cust_groups[info["custodian"] or os.path.basename(fp)].append((fp, info))
    for cust, items in sorted(cust_groups.items()):
        years = sorted({i["year"] for _, i in items if i["year"]})
        est = sum((i["messages"] or 0) for _, i in items)
        collections.append({
            "name": "{} — email".format(cust), "bucket": "pst",
            "track": "ediscovery", "custodian": cust, "parent_ref": None,
            "source_paths": [fp for fp, _ in items], "est_doc_count": est,
            "rationale": "PST mailbox(es) for '{}'{}; one collection per "
                         "custodian. {} msg across {} file(s).".format(
                             cust, " yrs " + ",".join(years) if years else "",
                             est, len(items))})

    # multi-part split archives
    for base, parts in sorted(split_groups.items()):
        parts.sort()
        collections.append({
            "name": "{} — production (split archive)".format(os.path.basename(base)),
            "bucket": "loadfile", "track": "ediscovery", "custodian": None,
            "parent_ref": None, "source_paths": [fp for _, fp in parts],
            "est_doc_count": None,
            "rationale": "Multi-part split archive ({} parts); reassemble then "
                         "load-file path.".format(len(parts))})

    # singleton zips: loadfile / dedup-provenance / fold-small / own collection
    fold_into_loose = defaultdict(lambda: {"zips": 0, "entries": 0, "bytes": 0})
    for fp, root, fn, sz in singleton_zips:
        zi = zip_info[fp]
        if not zi["enumerated"]:
            flagged.append({"path": fp, "type": "zip",
                            "reason": zi["error"] or "unreadable zip",
                            "suggested_action": "inspect manually before ingest"})
            continue
        overlap = sum(1 for k in zi["entry_index"] if k in loose_index)
        if zi["has_loadfile"]:
            collections.append({
                "name": "{} — production (load file)".format(os.path.splitext(fn)[0]),
                "bucket": "loadfile", "track": "ediscovery", "custodian": None,
                "parent_ref": None, "source_paths": [fp],
                "est_doc_count": zi["entries"],
                "rationale": "Zip contains load file(s) {}; load-file path.".format(
                    ", ".join(zi["loadfile_names"][:3]))})
        elif zi["entries"] and overlap >= max(1, int(0.5 * zi["entries"])):
            flagged.append({"path": fp, "type": "zip",
                            "reason": "{} of {} entries already loose (re-packaging)"
                                      .format(overlap, zi["entries"]),
                            "suggested_action": "keep as provenance; do not re-ingest"})
        elif (zi["entries"] or 0) <= SMALL_ZIP_ENTRIES:
            agg = fold_into_loose[root]
            agg["zips"] += 1
            agg["entries"] += zi["entries"] or 0
            agg["bytes"] += zi["uncompressed"]
        else:
            collections.append({
                "name": "{} — archive".format(os.path.splitext(fn)[0]),
                "bucket": "loose", "track": "client_files", "custodian": None,
                "parent_ref": None, "source_paths": [fp],
                "est_doc_count": zi["entries"],
                "rationale": "Zip with {} unique entries, no load file; own "
                             "collection.".format(zi["entries"])})

    # opaque archives / tars -> flagged
    for fp, root, fn, sz in (by_kind["tar"] + by_kind["tar_compressed"]
                             + by_kind["archive_opaque"]):
        flagged.append({"path": fp, "type": lower_ext(fn).lstrip("."),
                        "reason": "archive needs extraction to enumerate",
                        "suggested_action": "extract once on confirm; re-inventory"})

    # standalone load files on disk
    if by_kind["loadfile"]:
        lf_dirs = defaultdict(list)
        for fp, root, fn, sz in by_kind["loadfile"]:
            lf_dirs[os.path.dirname(fp)].append(fp)
        for d, lfs in sorted(lf_dirs.items()):
            collections.append({
                "name": "{} — production (load file)".format(os.path.basename(d) or d),
                "bucket": "loadfile", "track": "ediscovery", "custodian": None,
                "parent_ref": None, "source_paths": [d], "est_doc_count": None,
                "rationale": "Load file(s) {} on disk; load-file path.".format(
                    ", ".join(os.path.basename(x) for x in lfs[:3]))})

    # loose files -> one collection per root (small zips folded in)
    loose_by_root = defaultdict(lambda: {"n": 0, "bytes": 0, "zips": 0,
                                         "zip_entries": 0, "files": []})
    for fp, root, fn, sz in by_kind["loose"]:
        loose_by_root[root]["n"] += 1
        loose_by_root[root]["bytes"] += sz
        loose_by_root[root]["files"].append((fp, fn, sz))
    for root, z in fold_into_loose.items():
        loose_by_root[root]["zips"] += z["zips"]
        loose_by_root[root]["zip_entries"] += z["entries"]
        loose_by_root[root]["bytes"] += z["bytes"]
    loose_files_by_root = {}
    for root, agg in sorted(loose_by_root.items()):
        if agg["n"] == 0 and agg["zips"] == 0:
            continue
        zip_note = ""
        if agg["zips"]:
            zip_note = (" Folds in {} small archive(s) (~{} entries) as loose "
                        "content.".format(agg["zips"], agg["zip_entries"]))
        collections.append({
            "name": "{} — client files".format(
                os.path.basename(root.rstrip("/")) or root),
            "bucket": "loose", "track": "client_files",
            "custodian": "<producing party>", "parent_ref": None,
            "source_paths": [root], "est_doc_count": agg["n"] + agg["zip_entries"],
            "rationale": "{} loose document(s) ({}) under this root.{}".format(
                agg["n"], human(agg["bytes"]), zip_note)})
        loose_files_by_root[root] = agg["files"]

    for fp, root, fn, sz in by_kind["junk"]:
        flagged.append({"path": fp, "type": "junk",
                        "reason": "system/lock/temp file",
                        "suggested_action": "exclude from ingest"})

    placed_in_collections = sum(
        (c["est_doc_count"] or 0) for c in collections if c["bucket"] != "pst")
    inventory = {
        "roots": roots,
        "file_count": len(files),
        "total_bytes": total_bytes,
        "total_human": human(total_bytes),
        "by_kind": {k: len(v) for k, v in sorted(by_kind.items())},
        "ext_top": dict(sorted(ext_hist.items(), key=lambda kv: -kv[1])[:10]),
        "flagged_count": len(flagged),
        "collection_count": len(collections),
    }
    return {"inventory": inventory, "collections": collections,
            "flagged": flagged, "loose_files_by_root": loose_files_by_root}
