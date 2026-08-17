"""Load the FRPStudio bucket into the HARALD library.

    python tools/ingest_bucket.py --dry-run     what would happen, no downloads
    python tools/ingest_bucket.py --plan        download, classify, do not write
    python tools/ingest_bucket.py               load it

The bucket is the archive of everything iteria has bid, and its folder names
carry the one fact the old classifier kept getting wrong. Outcome was being
guessed from the filename and was right for 1 file in 202; it is stated
outright by the top-level prefix:

    02_WON_PROPOSALS          won
    03_IN_PROGRESS            in_progress
    04_LOST_PROPOSALS         lost
    SharePoint_Upload_NO-BID  no_bid
    01_BOILERPLATE            no outcome; not a bid
    ERPs written by the tool  test

Every file is catalogued, so the inventory is complete. Only iteria's own
writing is chunked, embedded and promoted into the answer library, because the
library is what the model reads to learn the house voice and a client's RFP is
the other side's voice. Loading those would teach it to write like the agency
it is bidding to.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import io
import json
import pathlib
import sys
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# The console defaults to cp1252 on Windows, and these filenames are full of
# em dashes and accented client names. Printing one raised UnicodeEncodeError
# and took the whole run down, an hour of loading lost to a progress line, with
# the document itself perfectly fine. Never let the log kill the job.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):  # not a real console; already fine
        pass

NAMESPACE = "bmi3vxyqnzrv"
BUCKET = "FRPStudio"
REGION = "ap-mumbai-1"

OUTCOMES = {
    "02_WON_PROPOSALS": "won",
    "03_IN_PROGRESS": "in_progress",
    "04_LOST_PROPOSALS": "lost",
    "SharePoint_Upload_NO-BID": "no_bid",
    "SharePoint_Upload_MSL": "no_bid",
    "01_BOILERPLATE": None,
    "ERPs written by the tool": "test",
}

# .pptx is 24 files and over half the bucket by size, and nothing here parses
# it. .zip and .png carry no prose. .doc is the pre-2007 binary format, which
# python-docx cannot open at all; those are named in the summary rather than
# silently dropped, because they are real proposals that are simply unreadable
# until someone resaves them.
READABLE = (".docx", ".pdf", ".xlsx", ".xlsm")
UNREADABLE = (".doc", ".xls")
IGNORED = (".pptx", ".ppt", ".zip", ".png", ".jpg")


def public_url(key: str) -> str:
    return (f"https://objectstorage.{REGION}.oraclecloud.com/n/{NAMESPACE}"
            f"/b/{BUCKET}/o/{urllib.parse.quote(key, safe='')}")


def listing() -> list[dict]:
    """Every object, by name and size."""
    base = f"https://objectstorage.{REGION}.oraclecloud.com/n/{NAMESPACE}/b/{BUCKET}/o"
    out, start = [], None
    while True:
        url = base + "?limit=1000&fields=name,size"
        if start:
            url += "&start=" + urllib.parse.quote(start)
        with urllib.request.urlopen(url, timeout=60) as response:
            page = json.load(response)
        out += page.get("objects", [])
        start = page.get("nextStartWith")
        if not start:
            return out


def reader():
    """Pick the way in once, and say which.

    The bucket is public today and should not stay that way, so both routes are
    supported. Deciding per object is what not to do: a failed credential
    lookup is not cached, so every file re-walks the whole chain and waits on an
    unreachable instance metadata endpoint. Five hundred files of that looks
    exactly like a hang, because for practical purposes it is one.
    """
    try:
        from app import ociclients
        client = ociclients.object_storage()
        client.get_namespace()
        print("  reading with OCI credentials")
        return lambda key: client.get_object(NAMESPACE, BUCKET, key).data.content
    except Exception as exc:  # noqa: BLE001
        print(f"  no OCI credentials ({str(exc).splitlines()[0][:60]}), "
              "reading anonymously")
        print("  this only works while the bucket is public")

        def anonymous(key: str) -> bytes:
            with urllib.request.urlopen(public_url(key), timeout=180) as response:
                return response.read()
        return anonymous


def metadata(key: str) -> dict:
    """Outcome, client and role, read from the path."""
    parts = key.split("/")
    prefix = parts[0]
    outcome = OUTCOMES.get(prefix, "in_progress")

    client = parts[1] if len(parts) > 2 else None
    if prefix == "01_BOILERPLATE":
        client = None

    lower = key.lower()
    if "cost" in lower or lower.endswith((".xlsx", ".xlsm")):
        role = "cost_workbook"
    elif "addendum" in lower:
        role = "addendum"
    elif "attachment" in lower:
        role = "attachment"
    elif "rfp" in lower and "iteria" not in lower:
        role = "rfp"
    elif "iteria" in lower:
        role = "iteria_response"
    else:
        role = "reference"

    return {"outcome": outcome, "client": client, "role": role, "prefix": prefix}


def summarise(rows: list[dict], heading: str) -> None:
    print(f"\n{heading}")
    by_prefix: dict[str, collections.Counter] = collections.defaultdict(
        collections.Counter)
    for row in rows:
        by_prefix[row["prefix"]][row["disposition"]] += 1
    width = max((len(p) for p in by_prefix), default=10)
    kinds = sorted({r["disposition"] for r in rows})
    print(f"  {'':<{width}}  " + "  ".join(f"{k:>12}" for k in kinds))
    for prefix in sorted(by_prefix):
        counts = by_prefix[prefix]
        print(f"  {prefix:<{width}}  "
              + "  ".join(f"{counts[k]:>12}" for k in kinds))
    total = collections.Counter(r["disposition"] for r in rows)
    print(f"  {'TOTAL':<{width}}  " + "  ".join(f"{total[k]:>12}" for k in kinds))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="classify from names only, download nothing")
    ap.add_argument("--plan", action="store_true",
                    help="download and classify from content, write nothing")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many readable files")
    args = ap.parse_args()

    objects = listing()
    print(f"{BUCKET}: {len(objects)} objects")

    rows = []
    for obj in objects:
        key = obj["name"]
        row = metadata(key) | {"key": key, "size": obj.get("size", 0)}
        lower = key.lower()
        if lower.endswith(IGNORED):
            row["disposition"] = "ignored"
        elif lower.endswith(UNREADABLE):
            row["disposition"] = "unreadable"
        elif lower.endswith(READABLE):
            row["disposition"] = "readable"
        else:
            row["disposition"] = "ignored"
        rows.append(row)

    summarise(rows, "By prefix, before reading any content:")

    unreadable = [r for r in rows if r["disposition"] == "unreadable"]
    if unreadable:
        print(f"\n{len(unreadable)} files are the pre-2007 .doc/.xls format and "
              "cannot be opened.\nResave them as .docx/.xlsx to include them:")
        for row in unreadable[:10]:
            print(f"    {row['key']}")

    todo = [r for r in rows if r["disposition"] == "readable"]
    if args.limit:
        todo = todo[:args.limit]
    megabytes = sum(r["size"] for r in todo) / 1e6
    print(f"\n{len(todo)} readable files, {megabytes:.0f} MB to download.")

    if args.dry_run:
        print("\n--dry-run: stopping before any download. The classifier needs "
              "the body\ntext to tell iteria's writing from a client's, so the "
              "library split is not\nknown yet. Use --plan for that.")
        return 0

    from app import chunking, classifier, embeddings
    from app.config import cfg

    fetch = reader()

    conn = cur = None
    if not args.plan:
        import oracledb

        from app import db
        pool = db.init_pool()
        conn = pool.acquire()
        cur = conn.cursor()
        cur.execute(f"ALTER SESSION SET CURRENT_SCHEMA = {cfg.app_schema}")

    seen: dict[str, str] = {}
    stats = collections.Counter()
    classified: list[dict] = []

    # Resume. Five hundred files at eight seconds each is over an hour, and an
    # hour-long job gets interrupted; without this, the second run would load
    # everything twice. Filenames already loaded are skipped before the download,
    # so resuming costs nothing.
    if cur is not None:
        cur.execute("SELECT filename, sha256 FROM harald_documents")
        for filename, digest in cur.fetchall():
            seen[digest] = filename
        done = set(seen.values())
        before = len(todo)
        todo = [r for r in todo if r["key"][:400] not in done]
        if before != len(todo):
            print(f"  resuming: {before - len(todo)} already loaded, "
                  f"{len(todo)} to go")

    # Downloads are the slow part and they do not need the CPU, so a few run
    # ahead of the extraction. Order is preserved: this overlaps the waiting,
    # it does not reorder the work.
    from concurrent.futures import ThreadPoolExecutor
    pool_size = 8
    with ThreadPoolExecutor(max_workers=pool_size) as downloads:
        pending: collections.deque = collections.deque()

        def top_up(next_index: int) -> int:
            while len(pending) < pool_size * 2 and next_index < len(todo):
                pending.append((todo[next_index],
                                downloads.submit(fetch, todo[next_index]["key"])))
                next_index += 1
            return next_index

        cursor_index = top_up(0)
        index = 0
        while pending:
            row, future = pending.popleft()
            cursor_index = top_up(cursor_index)
            index += 1
            key = row["key"]
            try:
                data = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"  [{index}/{len(todo)}] FETCH FAILED {key}: {exc}")
                stats["fetch_failed"] += 1
                continue

            digest = hashlib.sha256(data).hexdigest()
            if digest in seen:
                print(f"  [{index}/{len(todo)}] duplicate of {seen[digest]}")
                stats["duplicate"] += 1
                continue
            seen[digest] = key

            try:
                blocks = chunking.extract(key, data)
            except Exception as exc:  # noqa: BLE001
                print(f"  [{index}/{len(todo)}] EXTRACT FAILED {key}: "
                      f"{str(exc).splitlines()[0][:70]}")
                stats["extract_failed"] += 1
                continue

            text = chunking.plain_text(blocks)
            doc_class = classifier.classify(pathlib.PurePath(key).name, text)
            # Tool output is iteria's voice by construction, but its facts came
            # from a model and no one has checked them.
            trust = ("UNVERIFIED" if row["prefix"] == "ERPs written by the tool"
                     else "VERIFIED")
            library = (doc_class == "ITERIA_NARRATIVE"
                       and row["role"] != "cost_workbook")

            row |= {"doc_class": doc_class, "library": library,
                    "words": len(text.split())}
            classified.append(row)
            stats[doc_class] += 1

            mark = "LIBRARY" if library else "catalog"
            print(f"  [{index}/{len(todo)}] {mark:>7}  {doc_class:<17} "
                  f"{row['outcome'] or '-':<12} {pathlib.PurePath(key).name[:52]}")

            if args.plan:
                continue

            # Bind names are prefixed because several of the obvious ones are
            # Oracle reserved words, and the error for that is ORA-01745
            # "invalid host/bind variable name", which names neither the bind
            # nor the word it collided with.
            #
            # The LOB types are declared rather than inferred. Inference is
            # per-value and the cursor is reused, so a catalog document binding
            # None for the blob leaves that placeholder typed as a string, and
            # the next library document's .docx bytes are then decoded as UTF-8
            # and fail on the zip header. Declaring also lifts the 32767-byte
            # ceiling that doc_text would otherwise hit on a long RFP.
            cur.setinputsizes(b_blob=oracledb.DB_TYPE_BLOB,
                              b_text=oracledb.DB_TYPE_CLOB)
            doc_id = cur.var(int)
            cur.execute(
                """INSERT INTO harald_documents
                     (filename, doc_class, doc_role, client_name, outcome,
                      file_blob, size_bytes, sha256, doc_text, trust_level,
                      promoted_to_lib, uploaded_by)
                   VALUES (:b_fn, :b_cls, :b_role, :b_client, :b_outcome,
                           :b_blob, :b_size, :b_sha, :b_text, :b_trust,
                           :b_promo, 'bucket-ingest')
                   RETURNING doc_id INTO :b_out""",
                {"b_fn": key[:400], "b_cls": doc_class, "b_role": row["role"],
                 "b_client": (row["client"] or None),
                 "b_outcome": row["outcome"],
                 "b_blob": data if library else None, "b_size": len(data),
                 "b_sha": digest, "b_text": text,
                 "b_trust": trust, "b_promo": "Y" if library else "N",
                 "b_out": doc_id})

            if library:
                pieces = chunking.chunk(blocks)
                if pieces:
                    # Passage side of bge's asymmetric pair. Retrieval uses
                    # embed_query; mixing the two degrades every search.
                    vectors = embeddings.embed_passages(
                        [p["text"] for p in pieces])
                    cur.executemany(
                        """INSERT INTO harald_chunks
                             (doc_id, module_tag, section_tag, chunk_index,
                              chunk_text, token_count, tag_source, embedding)
                           VALUES (:b_doc, :b_mod, :b_sec, :b_idx, :b_text,
                                   :b_tok, :b_src, :b_vec)""",
                        [{"b_doc": doc_id.getvalue()[0], "b_mod": p["module"],
                          "b_sec": p["section"], "b_idx": i,
                          "b_text": p["text"], "b_tok": p["token_count"],
                          "b_src": p["tag_source"], "b_vec": v}
                         for i, (p, v) in enumerate(zip(pieces, vectors))])
                    stats["chunks"] += len(pieces)
            conn.commit()

    if classified:
        for row in classified:
            row["disposition"] = "library" if row["library"] else "catalog"
        summarise(classified, "By prefix, after reading the content:")

    print("\ntotals")
    for name, count in sorted(stats.items()):
        print(f"  {name:<20} {count}")

    if conn is not None:
        cur.execute("SELECT COUNT(*) FROM harald_documents")
        docs = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM harald_chunks")
        chunks = cur.fetchone()[0]
        cur.execute("""SELECT outcome, COUNT(*) FROM harald_documents
                        WHERE promoted_to_lib = 'Y' GROUP BY outcome
                        ORDER BY COUNT(*) DESC""")
        print(f"\nin the database: {docs} documents, {chunks} chunks")
        print("library documents by outcome:")
        for outcome, count in cur.fetchall():
            print(f"  {outcome or '(boilerplate)':<16} {count}")
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
