"""
Find dangling FHIR references (e.g. OrganizationAffiliation -> Location/4136336 that doesn't exist).

Usage:
    python check_refs.py H1619        # one contract
    python check_refs.py              # all contracts

Downloads each bundle once into ./cache/, then:
  Pass 1 - build a set of every (resourceType, id) that actually exists.
  Pass 2 - walk every resource for "reference" fields and flag targets missing from that set.

Outputs (per contract):
  dangling_refs_<C>.csv      one row per broken reference
  dangling_summary_<C>.csv   affected counts + % per source type -> target type
"""
import json, csv, re, os, sys, urllib.request, urllib.error, time
from datetime import date

CONTRACTS = {
    "H1619": "https://medicare-advantage-plan-finder-provider-directory.jeffersonhealthplans.com/h1619/2027/index.json",
    "H3124": "https://medicare-advantage-plan-finder-provider-directory.jeffersonhealthplans.com/h3124/2027/index.json",
    "H9207": "https://medicare-advantage-plan-finder-provider-directory.jeffersonhealthplans.com/h9207/2027/index.json",
    "H5826": "https://medicare-advantage-plan-finder-provider-directory.interop.chpw.org/h5826/2027/index.json",
}

# Prefix-agnostic: matches jhp-H1619-2027-location-part1.json AND any other org's
# <prefix>-<contract>-<year>-<category>-part<n>.json layout (e.g. CHPW's H5826 files).
FNAME_RE = re.compile(r"(?P<contract>[hH]\d+)-(?P<year>\d+)-(?P<category>[a-z]+)-part(?P<part>\d+)\.json", re.I)
# "Location/4136336", "Location/4136336/_history/1", or an absolute URL ending in Type/id
REF_RE = re.compile(r"(?:^|/)(?P<type>[A-Z][A-Za-z]+)/(?P<id>[A-Za-z0-9\-.]{1,64})(?:/_history/.*)?$")

CACHE = "cache"

PLACEHOLDER_EXT_URL = "http://hapifhir.io/fhir/StructureDefinition/resource-placeholder"


def has_placeholder_extension(resource):
    """True if the resource itself is explicitly marked placeholder data via the
    official 'resource-placeholder' extension (valueBoolean=true) -- the authoritative
    signal, as opposed to guessing from the shape of the id (see is_placeholder_id)."""
    for ext in resource.get("extension", []) or []:
        if ext.get("url") == PLACEHOLDER_EXT_URL and ext.get("valueBoolean") is True:
            return True
    return False

PLACEHOLDER_LITERALS = {
    "test", "example", "unknown", "tbd", "n/a", "na", "none", "null",
    "sample", "dummy", "fake", "todo", "xxx", "0000000000",
}


def is_placeholder_id(rid):
    """True if a target id looks like dummy/test data rather than a real FHIR id
    (all-zero, all-same-digit, simple sequential digits, or a known dummy literal),
    even though it may still happen to match a real resource in `existing`."""
    s = str(rid).strip()
    low = s.lower()
    if low in PLACEHOLDER_LITERALS:
        return True
    if s.isdigit():
        if len(set(s)) == 1:          # e.g. "0000", "1111"
            return True
        asc = "".join(str((int(s[0]) + i) % 10) for i in range(len(s)))
        desc = "".join(str((int(s[0]) - i) % 10) for i in range(len(s)))
        if len(s) >= 4 and s in (asc, desc):   # e.g. "1234", "9876"
            return True
    return False


def fetch(url, retries=6):
    """Small fetch (index files) fully into memory with retry/backoff."""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=300) as r:
                return r.read()
        except Exception as e:
            last = e
            wait = min(60, 3 * (2 ** i))
            print(f"    retry {i+1}/{retries} after {type(e).__name__}: {e} "
                  f"(waiting {wait}s)", flush=True)
            time.sleep(wait)
    raise last


def download_to(url, path, attempts=40):
    """Resumable streaming download. Writes to <path>.part, streaming in chunks so a
    283MB file never sits in memory. On a mid-stream drop it resumes with an HTTP
    Range request from the bytes already on disk instead of restarting from zero."""
    part = path + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    for i in range(attempts):
        try:
            headers = {"User-Agent": "Mozilla/5.0"}
            if have:
                headers["Range"] = f"bytes={have}-"
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=300) as r:
                status = r.status
                # Server ignored Range (200 = full body): restart the file cleanly.
                mode = "ab" if (have and status == 206) else "wb"
                if mode == "wb":
                    have = 0
                total = r.length  # remaining bytes for this response, may be None
                got = 0
                with open(part, mode) as f:
                    while True:
                        chunk = r.read(1 << 20)  # 1 MB
                        if not chunk:
                            break
                        f.write(chunk)
                        got += len(chunk)
                # If the server told us a length and we got it all, we're done.
                if total is None or got >= total:
                    os.replace(part, path)
                    return
                have = os.path.getsize(part)  # short read; loop to resume
                print(f"    short read ({have:,} B so far), resuming...", flush=True)
        except urllib.error.HTTPError as e:
            # 416 = our .part is bigger than the server's file (stale/corrupt partial).
            # Throw it away and restart from zero instead of looping on a bad range.
            if e.code == 416 and os.path.exists(part):
                os.remove(part)
                have = 0
                print(f"    stale partial (HTTP 416); discarded, restarting from 0",
                      flush=True)
                continue
            have = os.path.getsize(part) if os.path.exists(part) else 0
            wait = min(60, 3 * (2 ** min(i, 4)))
            print(f"    retry {i+1}/{attempts} after HTTPError {e.code}: "
                  f"({have:,} B on disk, waiting {wait}s)", flush=True)
            time.sleep(wait)
        except Exception as e:
            have = os.path.getsize(part) if os.path.exists(part) else 0
            wait = min(60, 3 * (2 ** min(i, 4)))
            print(f"    retry {i+1}/{attempts} after {type(e).__name__}: {e} "
                  f"({have:,} B on disk, waiting {wait}s)", flush=True)
            time.sleep(wait)
    raise RuntimeError(f"Failed to fully download {url} after {attempts} attempts")


def cached_bytes(url, fname):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, fname)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as f:
            return f.read()
    download_to(url, path)
    with open(path, "rb") as f:
        return f.read()


def meta_path(contract):
    return os.path.join(CACHE, f"_last_updated_{contract}.txt")


def ensure_fresh(contract, files, live_updated, force):
    """Auto-invalidate the cache when the provider republishes.

    We stamp each contract's cached data with the index 'last_updated'. If the live
    value differs (or --fresh is given), delete this contract's cached files so they
    re-download. Without this, a re-run silently reports on a stale snapshot and you
    cannot tell whether a reported issue was actually fixed upstream."""
    os.makedirs(CACHE, exist_ok=True)
    mp = meta_path(contract)
    prev = None
    if os.path.exists(mp):
        with open(mp, encoding="utf-8") as f:
            prev = f.read().strip()
    if force or (prev is not None and prev != str(live_updated)):
        reason = "forced --fresh" if force else f"data changed ({prev} -> {live_updated})"
        print(f"Cache invalidated: {reason}. Re-downloading this contract.", flush=True)
        for _, fname, _, _ in files:
            for p in (os.path.join(CACHE, fname), os.path.join(CACHE, fname + ".part")):
                if os.path.exists(p):
                    os.remove(p)
    with open(mp, "w", encoding="utf-8") as f:
        f.write(str(live_updated))


def resources_of(raw):
    """Yield each FHIR resource from a Bundle (or a bare list/object)."""
    data = json.loads(raw)
    if isinstance(data, dict) and data.get("resourceType") == "Bundle":
        for e in data.get("entry", []):
            r = e.get("resource", e)
            if isinstance(r, dict):
                yield r
    elif isinstance(data, list):
        for r in data:
            if isinstance(r, dict):
                yield r
    elif isinstance(data, dict):
        yield data


def find_refs(node, path=""):
    """Recursively yield (json_path, reference_string) for every 'reference' field."""
    if isinstance(node, dict):
        for k, v in node.items():
            p = f"{path}.{k}" if path else k
            if k == "reference" and isinstance(v, str):
                yield p, v
            else:
                yield from find_refs(v, p)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from find_refs(v, f"{path}[{i}]")


def field_root(path):
    """'location[0].reference' -> 'location'; 'coverageArea[3].reference' -> 'coverageArea'.
    Different fields mean different things (location vs coverageArea), so they are
    reported separately rather than collapsed into one source->target row."""
    p = re.sub(r"\[\d+\]", "", path)
    if p.endswith(".reference"):
        p = p[: -len(".reference")]
    return p or "reference"


def parse_ref(ref):
    """Return (type, id) for a resolvable reference, else None for contained/urn refs."""
    if not ref or ref.startswith("#") or ref.startswith("urn:"):
        return None
    m = REF_RE.search(ref.strip())
    if not m:
        return None
    return m.group("type"), m.group("id")


def run(contract, index_url):
    print(f"\n=== {contract} ===", flush=True)
    idx = json.loads(fetch(index_url))
    urls = idx.get("provider_urls", [])
    print(f"{len(urls)} files (last_updated={idx.get('last_updated')})", flush=True)

    files = []  # (fname, category, part)
    for url in urls:
        fname = url.rsplit("/", 1)[-1]
        m = FNAME_RE.search(fname)
        files.append((url, fname,
                      m.group("category") if m else "unknown",
                      int(m.group("part")) if m else 0))

    # Drop stale cache if the provider republished (or --fresh was passed).
    ensure_fresh(contract, files, idx.get("last_updated"), FORCE_FRESH)

    # ---- Pass 1: every (resourceType, id) that exists ----
    print("Pass 1: indexing existing resource ids ...", flush=True)
    existing = set()
    identifiers = {}              # (resourceType, id) -> "system|value" identifiers, joined by "; "
    raw_type_counts = {}          # includes duplicate ids (raw entry count)
    ext_placeholder_by_file = {}  # fname -> count of resources with resource-placeholder ext
    for url, fname, category, part in files:
        raw = cached_bytes(url, fname)
        n = 0
        for r in resources_of(raw):
            rt, rid = r.get("resourceType"), r.get("id")
            if rt and rid is not None and str(rid) != "":
                existing.add((rt, str(rid)))
                raw_type_counts[rt] = raw_type_counts.get(rt, 0) + 1
                idents = [f"{i.get('system', '')}|{i.get('value', '')}"
                          for i in (r.get("identifier") or []) if isinstance(i, dict)]
                if idents:
                    identifiers[(rt, str(rid))] = "; ".join(idents)
            if has_placeholder_extension(r):
                ext_placeholder_by_file[fname] = ext_placeholder_by_file.get(fname, 0) + 1
            n += 1
        print(f"  {fname}: {n}", flush=True)

    if ext_placeholder_by_file:
        print(f"\nResources marked with resource-placeholder extension, by file:")
        for fname in sorted(ext_placeholder_by_file):
            print(f"  {fname}: {ext_placeholder_by_file[fname]:,}")
        print(f"Total: {sum(ext_placeholder_by_file.values()):,}")
    # Unique count per type = correct denominator for "% of source affected".
    type_counts = {}
    for t, _ in existing:
        type_counts[t] = type_counts.get(t, 0) + 1
    published_types = set(type_counts)
    dups = sum(raw_type_counts[t] - type_counts.get(t, 0) for t in raw_type_counts)
    print(f"Indexed {len(existing)} unique resources"
          f"{f' ({dups} duplicate ids collapsed)' if dups else ''}.", flush=True)

    # ---- Write resource counts (total published per type, from the source JSON) ----
    with open(f"resource_counts_{contract}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["resource_type", "unique_count", "raw_count"])
        for t in sorted(type_counts):
            w.writerow([t, type_counts[t], raw_type_counts.get(t, type_counts[t])])

    # ---- Pass 2: check every reference ----
    print("Pass 2: checking references ...", flush=True)
    dangling = []                 # source_type, source_id, file, field, target_type, target_id
    affected = {}                 # (src_type, field, tgt_type) -> source ids with >=1 broken ref
    ref_totals = {}               # (src_type, field, tgt_type) -> total refs seen
    src_with_any_ref = {}         # (src_type, field, tgt_type) -> source ids having such a ref
    bad_ref_counts = {}           # (src_type, field, tgt_type) -> broken ref count
    placeholder_refs = []         # src_type, src_id, file, field, target_type, target_id, connected(bool)
    placeholder_connected = {}    # (src_type, field, tgt_type) -> count where placeholder id resolved
    placeholder_total = {}        # (src_type, field, tgt_type) -> count of placeholder-looking ids seen
    referenced_ids = {}           # (src_type, field, tgt_type) -> set of target ids referenced (any status)

    for url, fname, category, part in files:
        raw = cached_bytes(url, fname)
        for r in resources_of(raw):
            src_type, src_id = r.get("resourceType", ""), str(r.get("id", ""))
            for field, ref in find_refs(r):
                parsed = parse_ref(ref)
                if not parsed:
                    continue
                tgt_type, tgt_id = parsed
                key = (src_type, field_root(field), tgt_type)
                ref_totals[key] = ref_totals.get(key, 0) + 1
                src_with_any_ref.setdefault(key, set()).add(src_id)
                referenced_ids.setdefault(key, set()).add(tgt_id)
                connected = (tgt_type, tgt_id) in existing
                if not connected:
                    dangling.append([src_type, src_id, fname, field, tgt_type, tgt_id, ref])
                    affected.setdefault(key, set()).add(src_id)
                    bad_ref_counts[key] = bad_ref_counts.get(key, 0) + 1
                if is_placeholder_id(tgt_id):
                    placeholder_refs.append([src_type, src_id, fname, field, tgt_type, tgt_id,
                                              "yes" if connected else "no"])
                    placeholder_total[key] = placeholder_total.get(key, 0) + 1
                    if connected:
                        placeholder_connected[key] = placeholder_connected.get(key, 0) + 1

    # ---- Write per-reference detail ----
    with open(f"dangling_refs_{contract}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source_type", "source_id", "source_file", "field_path",
                    "target_type", "target_id", "raw_reference"])
        w.writerows(dangling)

    # ---- Write placeholder-id detail (dummy-looking ids, connected or not) ----
    with open(f"placeholder_refs_{contract}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source_type", "source_id", "source_file", "field_path",
                    "target_type", "target_id", "connected"])
        w.writerows(placeholder_refs)

    # ---- Write summary (per source.field -> target) ----
    rows = []
    for key in sorted(set(list(ref_totals) + list(affected))):
        src_type, field, tgt_type = key
        n_bad_refs = bad_ref_counts.get(key, 0)
        n_affected = len(affected.get(key, ()))
        n_src_total = type_counts.get(src_type, 0)
        n_src_with_ref = len(src_with_any_ref.get(key, ()))
        pct_of_all = (100.0 * n_affected / n_src_total) if n_src_total else 0.0
        pct_of_linked = (100.0 * n_affected / n_src_with_ref) if n_src_with_ref else 0.0
        # "yes" = target type is published but specific ids are missing;
        # "no"  = that whole resource type was never published in this contract.
        tgt_published = "yes" if tgt_type in published_types else "no"
        rows.append([src_type, field, tgt_type, tgt_published, ref_totals.get(key, 0),
                     n_bad_refs, n_affected, n_src_total, f"{pct_of_all:.2f}",
                     n_src_with_ref, f"{pct_of_linked:.2f}"])

    with open(f"dangling_summary_{contract}.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source_type", "field", "target_type", "target_type_published",
                    "total_refs", "dangling_refs",
                    "affected_source_resources", "total_source_resources", "pct_of_all_source",
                    "source_resources_with_this_ref", "pct_of_linked_source"])
        w.writerows(rows)

    # ---- Console report ----
    print(f"\n--- {contract} dangling references (by field) ---")
    hdr = (f"{'SOURCE.FIELD':<42} {'TARGET':<18} {'PUB':>4} {'REFS':>10} "
           f"{'BROKEN':>10} {'AFFECTED':>9} {'%TYPE':>7}")
    print(hdr)
    print("-" * len(hdr))
    for (src_type, field, tgt_type, tgt_pub, total, bad,
         aff, src_total, pct, _, _) in rows:
        if bad:
            print(f"{src_type + '.' + field:<42} {tgt_type:<18} {tgt_pub:>4} {total:>10,} "
                  f"{bad:>10,} {aff:>9,} {pct:>6}%")
    clean = [r for r in rows if not r[5]]
    if clean:
        print("\nClean (no dangling refs):")
        for src_type, field, tgt_type, *_ in clean:
            print(f"  {src_type}.{field} -> {tgt_type}: all resolve")
    missing_types = sorted({r[2] for r in rows if r[3] == "no" and r[5]})
    if missing_types:
        print(f"\nTarget types referenced but NEVER published: {', '.join(missing_types)}")

    if placeholder_refs:
        print(f"\n--- {contract} placeholder-looking target ids ---")
        phdr = f"{'SOURCE.FIELD':<42} {'TARGET':<18} {'PLACEHOLDER':>11} {'CONNECTED':>10}"
        print(phdr)
        print("-" * len(phdr))
        for key in sorted(set(list(placeholder_total))):
            src_type, field, tgt_type = key
            tot = placeholder_total.get(key, 0)
            conn = placeholder_connected.get(key, 0)
            print(f"{src_type + '.' + field:<42} {tgt_type:<18} {tot:>11,} {conn:>10,}")

        # Roll up by target resource type, across every field that references it.
        by_type_total, by_type_conn = {}, {}
        for row in placeholder_refs:
            _, _, _, _, tgt_type, _, connected = row
            by_type_total[tgt_type] = by_type_total.get(tgt_type, 0) + 1
            if connected == "yes":
                by_type_conn[tgt_type] = by_type_conn.get(tgt_type, 0) + 1
        print(f"\nPlaceholder counts by resource type (all fields combined):")
        thdr = f"{'RESOURCE TYPE':<20} {'PLACEHOLDER':>11} {'CONNECTED':>10} {'DANGLING':>9}"
        print(thdr)
        print("-" * len(thdr))
        for tgt_type in sorted(by_type_total):
            tot = by_type_total[tgt_type]
            conn = by_type_conn.get(tgt_type, 0)
            print(f"{tgt_type:<20} {tot:>11,} {conn:>10,} {tot - conn:>9,}")

        n_conn = sum(placeholder_connected.values())
        n_not_conn = len(placeholder_refs) - n_conn
        print(f"\nTotal placeholder-looking ids: {len(placeholder_refs):,} "
              f"({n_conn:,} still resolve to a real resource, "
              f"{n_not_conn:,} are dangling)")

        if n_not_conn:
            print(f"\nNOT CONNECTED placeholder ids (dangling): {n_not_conn:,}")
            not_conn_by_type = {}
            for row in placeholder_refs:
                _, _, _, _, tgt_type, _, connected = row
                if connected == "no":
                    not_conn_by_type[tgt_type] = not_conn_by_type.get(tgt_type, 0) + 1
            for tgt_type in sorted(not_conn_by_type):
                print(f"  {tgt_type}: {not_conn_by_type[tgt_type]:,}")

        # Placeholder count per source JSON file, standalone (no connected/dangling split).
        by_file = {}
        for row in placeholder_refs:
            fname = row[2]
            by_file[fname] = by_file.get(fname, 0) + 1
        print(f"\nPlaceholder count by source file:")
        for fname in sorted(by_file):
            print(f"  {fname}: {by_file[fname]:,}")

        # End-to-end: InsurancePlan -> Organization specifically (its own chain,
        # not mixed in with every other source type that also references Organization).
        ip_org = [r for r in placeholder_refs
                  if r[0] == "InsurancePlan" and r[4] == "Organization"]
        if ip_org:
            ip_conn = sum(1 for r in ip_org if r[6] == "yes")
            ip_not_conn = len(ip_org) - ip_conn
            print(f"\nEnd-to-end: InsurancePlan -> Organization placeholder ids: "
                  f"{len(ip_org):,} total ({ip_conn:,} connected, {ip_not_conn:,} not connected)")
            for src_type, src_id, fname, field, tgt_type, tgt_id, connected in ip_org:
                print(f"  InsurancePlan/{src_id} --{field}--> Organization/{tgt_id} "
                      f"[{('connected' if connected == 'yes' else 'NOT CONNECTED')}]")

        # End-to-end: OrganizationAffiliation -> Organization specifically.
        oa_org = [r for r in placeholder_refs
                  if r[0] == "OrganizationAffiliation" and r[4] == "Organization"]
        if oa_org:
            oa_conn = sum(1 for r in oa_org if r[6] == "yes")
            oa_not_conn = len(oa_org) - oa_conn
            print(f"\nEnd-to-end: OrganizationAffiliation -> Organization placeholder ids: "
                  f"{len(oa_org):,} total ({oa_conn:,} connected, {oa_not_conn:,} not connected)")
            for src_type, src_id, fname, field, tgt_type, tgt_id, connected in oa_org:
                print(f"  OrganizationAffiliation/{src_id} --{field}--> Organization/{tgt_id} "
                      f"[{('connected' if connected == 'yes' else 'NOT CONNECTED')}]")

        # End-to-end: PractitionerRole -> Practitioner specifically.
        pr_prac = [r for r in placeholder_refs
                   if r[0] == "PractitionerRole" and r[4] == "Practitioner"]
        if pr_prac:
            pr_conn = sum(1 for r in pr_prac if r[6] == "yes")
            pr_not_conn = len(pr_prac) - pr_conn
            print(f"\nEnd-to-end: PractitionerRole -> Practitioner placeholder ids: "
                  f"{len(pr_prac):,} total ({pr_conn:,} connected, {pr_not_conn:,} not connected)")
            for src_type, src_id, fname, field, tgt_type, tgt_id, connected in pr_prac:
                print(f"  PractitionerRole/{src_id} --{field}--> Practitioner/{tgt_id} "
                      f"[{('connected' if connected == 'yes' else 'NOT CONNECTED')}]")

    # ---- Reverse/orphan checks: published resources never referenced back ----
    # e.g. an Organization with no OrganizationAffiliation.organization pointing to it,
    # or a Practitioner with no PractitionerRole.practitioner pointing to it.
    ORPHAN_CHECKS = [
        ("Organization", "OrganizationAffiliation", "organization"),
        ("Practitioner", "PractitionerRole", "practitioner"),
    ]
    orphan_rows = []
    for tgt_type, ref_src_type, field in ORPHAN_CHECKS:
        key = (ref_src_type, field, tgt_type)
        referenced = referenced_ids.get(key, set())
        all_ids = {rid for (rt, rid) in existing if rt == tgt_type}
        orphans = sorted(all_ids - referenced)
        if not all_ids:
            continue
        print(f"\n--- {contract}: {tgt_type} not referenced by any {ref_src_type}.{field} ---")
        print(f"{len(orphans):,} of {len(all_ids):,} {tgt_type} resources are orphaned "
              f"({100.0 * len(orphans) / len(all_ids):.2f}%)")
        for oid in orphans:
            orphan_rows.append([tgt_type, oid, identifiers.get((tgt_type, oid), ""),
                                 ref_src_type, field])

    if orphan_rows:
        with open(f"orphan_refs_{contract}.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["orphan_type", "orphan_id", "identifier",
                        "expected_referencing_type", "expected_field"])
            w.writerows(orphan_rows)

    print(f"\nTotal dangling references: {len(dangling):,}")
    print(f"Wrote: dangling_refs_{contract}.csv, dangling_summary_{contract}.csv"
          + (f", placeholder_refs_{contract}.csv" if placeholder_refs else "")
          + (f", orphan_refs_{contract}.csv" if orphan_rows else ""))
    return len(dangling)


def _read_csv(path):
    if not os.path.exists(path):
        return [], []
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    return (rows[0], rows[1:]) if rows else ([], [])


def build_report(contracts):
    """After each contract's own run has written its CSVs, roll them into one
    manager-facing report: an Excel workbook (per-contract detail tabs) and a
    Word summary. Each contract's numbers still come from its own separate run;
    this just collates the already-written per-contract outputs."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
        from docx import Document
    except ImportError as e:
        print(f"\nSkipping report generation (missing dependency: {e}). "
              f"Run: pip install openpyxl python-docx")
        return

    data = {}
    for c in contracts:
        shdr, srows = _read_csv(f"dangling_summary_{c}.csv")
        ohdr, orows = _read_csv(f"orphan_refs_{c}.csv")
        chdr, crows = _read_csv(f"resource_counts_{c}.csv")
        phdr, prows = _read_csv(f"placeholder_refs_{c}.csv")
        total_dangling = sum(int(r[5]) for r in srows) if srows else 0
        total_affected = sum(int(r[6]) for r in srows) if srows else 0
        # placeholder rollup by target resource type: total seen, connected, dangling
        ph_by_type = {}
        for row in prows:
            _, _, _, _, tgt_type, _, connected = row
            e = ph_by_type.setdefault(tgt_type, {"total": 0, "connected": 0})
            e["total"] += 1
            if connected == "yes":
                e["connected"] += 1
        org_orphans = sum(1 for r in orows if r[0] == "Organization")
        prac_orphans = sum(1 for r in orows if r[0] == "Practitioner")
        data[c] = dict(shdr=shdr, srows=srows, ohdr=ohdr, orows=orows,
                        crows=crows, ph_by_type=ph_by_type,
                        total_dangling=total_dangling, total_affected=total_affected,
                        org_orphans=org_orphans, prac_orphans=prac_orphans)

    grand_dangling = sum(d["total_dangling"] for d in data.values())
    grand_orphans = sum(len(d["orows"]) for d in data.values())

    # ---- Excel ----
    wb = Workbook()
    HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
    HEADER_FONT = Font(color="FFFFFF", bold=True)
    BAD_FILL = PatternFill("solid", fgColor="FCE4E4")
    OK_FILL = PatternFill("solid", fgColor="E4F7E4")

    def style_header(ws, row=1, ncols=1):
        for col in range(1, ncols + 1):
            cell = ws.cell(row=row, column=col)
            cell.fill = HEADER_FILL
            cell.font = HEADER_FONT
            cell.alignment = Alignment(horizontal="center", vertical="center")

    def autosize(ws, ncols):
        for col in range(1, ncols + 1):
            letter = get_column_letter(col)
            maxlen = max((len(str(c.value)) if c.value is not None else 0)
                         for c in ws[letter])
            ws.column_dimensions[letter].width = min(max(maxlen + 2, 10), 60)

    ws = wb.active
    ws.title = "Overview"
    ws.append(["FHIR Provider Directory - Reference Integrity Report"])
    ws["A1"].font = Font(size=14, bold=True)
    ws.append([f"Generated: {date.today().isoformat()}"])
    ws.append([])
    ws.append(["Contract", "Dangling References", "Orphaned Organizations",
               "Total Referenced Resources", "Status"])
    style_header(ws, row=4, ncols=5)
    for c in contracts:
        d = data[c]
        total_refs = sum(int(r[4]) for r in d["srows"]) if d["srows"] else 0
        status = "CLEAN" if d["total_dangling"] == 0 else "ISSUES FOUND"
        ws.append([c, d["total_dangling"], len(d["orows"]), total_refs, status])
        r = ws.max_row
        fill = OK_FILL if d["total_dangling"] == 0 else BAD_FILL
        for col in range(1, 6):
            ws.cell(row=r, column=col).fill = fill
    ws.append([])
    ws.append(["TOTAL (all contracts)", grand_dangling, grand_orphans])
    ws[f"A{ws.max_row}"].font = Font(bold=True)
    autosize(ws, 5)

    for c in contracts:
        d = data[c]
        wsum = wb.create_sheet(f"{c} Summary")
        if d["shdr"]:
            wsum.append(d["shdr"])
            style_header(wsum, row=1, ncols=len(d["shdr"]))
            for row in d["srows"]:
                wsum.append(row)
                rr = wsum.max_row
                if int(row[5]) > 0:
                    for col in range(1, len(d["shdr"]) + 1):
                        wsum.cell(row=rr, column=col).fill = BAD_FILL
            autosize(wsum, len(d["shdr"]))
        wsum.freeze_panes = "A2"

        worp = wb.create_sheet(f"{c} Orphans")
        if d["ohdr"]:
            worp.append(d["ohdr"])
            style_header(worp, row=1, ncols=len(d["ohdr"]))
            for row in d["orows"]:
                worp.append(row)
            autosize(worp, len(d["ohdr"]))
        else:
            worp.append(["No orphaned resources found."])
        worp.freeze_panes = "A2"

    xlsx_path = "FHIR_Reference_Integrity_Report.xlsx"
    wb.save(xlsx_path)

    # ---- Word ----
    def add_table(doc, headers, rows, style="Light Grid Accent 1"):
        t = doc.add_table(rows=1, cols=len(headers))
        t.style = style
        for i, h in enumerate(headers):
            cell = t.rows[0].cells[i]
            cell.text = h
            cell.paragraphs[0].runs[0].font.bold = True
        for row in rows:
            cells = t.add_row().cells
            for i, v in enumerate(row):
                cells[i].text = str(v)
        return t

    today_str = date.today().strftime("%d-%b-%Y")

    doc = Document()
    doc.add_heading("Reference Integrity Validation Report", level=0)
    doc.add_paragraph(f"Execution Date: {today_str}")
    doc.add_paragraph("Validation Type: Forward Reference / Dangling Reference Validation")
    p = doc.add_paragraph()
    p.add_run("Contracts covered: ").bold = True
    p.add_run(", ".join(contracts))

    overall_status = "PASS" if grand_dangling == 0 else "FAIL"
    doc.add_heading("1. Overall Summary", level=1)
    add_table(doc, ["Metric", "Result"], [
        ["Execution Date", today_str],
        ["Environment", "Production"],
        ["Validation Type", "Forward Reference / Dangling Reference"],
        ["Contracts Validated", len(contracts)],
        ["Dangling References", f"{grand_dangling:,}"],
        ["Affected Source Resources",
         f"{sum(d['total_affected'] for d in data.values()):,}"],
        ["Orphaned Organizations", f"{grand_orphans:,}"],
        ["Reference Integrity", overall_status],
    ])

    for c in contracts:
        d = data[c]
        status = "PASS" if d["total_dangling"] == 0 else "FAIL"
        n_relationships = len(d["srows"])

        doc.add_heading(f"Contract: {c}", level=1)

        doc.add_heading("1. Execution Summary", level=2)
        add_table(doc, ["Metric", "Result"], [
            ["Execution Date", today_str],
            ["Environment", "Production"],
            ["Validation Type", "Forward Reference / Dangling Reference"],
            ["Relationship Checks", n_relationships],
            ["Dangling References", f"{d['total_dangling']:,}"],
            ["Affected Source Resources", f"{d['total_affected']:,}"],
            ["Reference Integrity", status],
        ])

        doc.add_heading("2. Validation Results", level=2)
        vrows = []
        for row in d["srows"]:
            src_type, field, tgt_type, tgt_pub, total, bad, aff = row[:7]
            vrows.append([src_type, field, tgt_type, f"{int(total):,}",
                          bad, aff, "PASS" if int(bad) == 0 else "FAIL"])
        add_table(doc, ["Source Type", "Field", "Target Type", "Total References",
                        "Dangling", "Affected Resources", "Result"], vrows)

        doc.add_heading("3. Resource Counts (Total in JSON)", level=2)
        if d["crows"]:
            add_table(doc, ["Resource Type", "Unique Count", "Raw Count"], d["crows"])
        else:
            doc.add_paragraph("No resource-count data available for this contract.")

        doc.add_heading("4. Placeholder-Looking Target IDs", level=2)
        if d["ph_by_type"]:
            ph_rows = []
            for tgt_type in sorted(d["ph_by_type"]):
                e = d["ph_by_type"][tgt_type]
                dangling_ph = e["total"] - e["connected"]
                ph_rows.append([tgt_type, e["total"], e["connected"], dangling_ph])
            add_table(doc, ["Target Type", "Placeholder-Looking", "Connected", "Dangling"],
                      ph_rows)
        else:
            doc.add_paragraph("No placeholder-looking ids found.")

        doc.add_heading("5. Orphan Connectivity Checks", level=2)
        org_status = "Pass" if d["org_orphans"] == 0 else f"Pass ({d['org_orphans']} orphaned)"
        prac_status = "Pass" if d["prac_orphans"] == 0 else f"Pass ({d['prac_orphans']} orphaned)"
        doc.add_paragraph(f"Organization is connected to OrganizationAffiliation - {org_status}")
        doc.add_paragraph(f"Practitioner is connected to PractitionerRole - {prac_status}")

        doc.add_heading("6. Orphan Details", level=2)
        if d["orows"]:
            add_table(doc, ["Orphan Type", "Orphan ID", "Identifier",
                            "Expected Referencing Type", "Expected Field"], d["orows"])
        else:
            doc.add_paragraph("No orphaned resources found for this contract.")

        doc.add_paragraph()

    doc.add_heading("Recommendation", level=1)
    doc.add_paragraph(
        "Review the orphaned records above with the data provider to confirm "
        "whether they are intentionally unaffiliated or should be linked via an "
        "OrganizationAffiliation / PractitionerRole entry." if grand_orphans else
        "No follow-up required -- all references resolve and no orphaned "
        "resources were found."
    )
    doc.add_paragraph()
    foot = doc.add_paragraph()
    foot.add_run("Full detail (per-reference breakdowns) is available in the "
                 "accompanying Excel workbook: "
                 "FHIR_Reference_Integrity_Report.xlsx").italic = True

    docx_path = "FHIR_Reference_Integrity_Report.docx"
    doc.save(docx_path)

    print(f"\nWrote report: {xlsx_path}, {docx_path}")


# CLI: python check_refs.py [CONTRACT ...] [--fresh]
#   CONTRACT  one or more contracts, each run as its own separate pass (default: all)
#   --fresh   force re-download, ignoring cached files (else cache auto-refreshes
#             whenever the provider's index last_updated changes)
args = [a for a in sys.argv[1:]]
FORCE_FRESH = any(a.lower() in ("--fresh", "-f") for a in args)
positional = [a for a in args if not a.startswith("-")]

if positional:
    targets = {}
    for a in positional:
        code = a.upper()
        if code not in CONTRACTS:
            print(f"Unknown contract '{a}'. Choose from: {', '.join(CONTRACTS)}")
            sys.exit(1)
        targets[code] = CONTRACTS[code]
else:
    targets = CONTRACTS

grand_total = 0
for c, u in targets.items():
    grand_total += run(c, u)
if len(targets) > 1:
    print(f"\n==== ALL CONTRACTS: {grand_total:,} total dangling references ====")

build_report(list(targets))
