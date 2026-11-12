"""Copy finished days to the archive (Cloudflare R2) and keep only recent data on the Pi.

    python3 -m aircraft_noise.archive                    # nightly by cron: upload, then prune
    python3 -m aircraft_noise.archive --dry-run          # say what it would do, change nothing
    python3 -m aircraft_noise.archive --restore OUT.db   # rebuild a database from the archive

Each UTC day becomes one gzipped CSV per table, e.g. levels/2026-11-07.csv.gz, written once and
never changed. Every upload is checked on R2 (size and MD5) and recorded in the `archived` table.
A day's levels and aircraft positions (99% of the space) are deleted from the Pi once the day is
KEEP_DAYS old, and only after their files are checked on R2 again in that same run; passes and
logger_coverage are small and stay. `meta` holds the home location and is never uploaded.

The bucket and its keys are in ARCHIVE_CONFIG (see the README). R2 speaks the S3 API, used
through boto3 (on the Pi: apt install python3-boto3).
"""

import argparse
import base64
import calendar
import csv
import datetime as dt
import gzip
import hashlib
import io
import json
import logging
import shutil
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import (ARCHIVE_AFTER_S, ARCHIVE_CONFIG, DATA_DIR, KEEP_DAYS, home_location)
from .db import connect, set_meta

log = logging.getLogger("archive")

# table -> (time column that puts a row in a day, deleted from the Pi once archived)
TABLES = {
    "levels": ("ts", True),
    "aircraft_positions": ("ts", True),
    "passes": ("closest_ts", False),
    "logger_coverage": ("start_ts", False),   # periods are cut at midnight, so each day is whole
}
DAY_S = 86400


# --- R2 (S3 API) ------------------------------------------------------------------------------

class R2:
    """The few bucket operations the archive needs: put, head, get and list (boto3, S3 API)."""

    def __init__(self, config_path=ARCHIVE_CONFIG):
        import boto3   # here, so a dry run (and the collection services) don't need it
        try:
            cfg = json.loads(Path(config_path).read_text())
            self.bucket = cfg["bucket"]
            self.s3 = boto3.client(
                "s3", endpoint_url=f"https://{cfg['account_id']}.r2.cloudflarestorage.com",
                aws_access_key_id=cfg["access_key_id"],
                aws_secret_access_key=cfg["secret_access_key"], region_name="auto")
        except (OSError, ValueError, KeyError) as e:
            raise SystemExit(f'Need {config_path} like {{"account_id": .., "bucket": .., '
                             f'"access_key_id": .., "secret_access_key": ..}} (see README): {e}')

    def put(self, key, body):
        md5 = base64.b64encode(hashlib.md5(body).digest()).decode()
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body,
                           ContentType="application/gzip", ContentMD5=md5)

    def head(self, key):
        """(size, md5) of the stored file, or None if it isn't there."""
        from botocore.exceptions import ClientError
        try:
            r = self.s3.head_object(Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return r["ContentLength"], r["ETag"].strip('"')

    def get(self, key):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.s3.exceptions.NoSuchKey:
            raise SystemExit(f"{key} is missing from the archive")

    def keys(self, prefix=""):
        """Every key in the bucket starting with prefix."""
        pages = self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix)
        for page in pages:
            yield from (obj["Key"] for obj in page.get("Contents", []))

    def verified(self, key, size, md5):
        return self.head(key) == (size, md5)


# --- Days -------------------------------------------------------------------------------------

def day_start(day):
    return calendar.timegm(day.timetuple())


def file_key(table, day):
    return f"{table}/{day.isoformat()}.csv.gz"


def mics(db):
    """The mic names in levels, found through its (mic, ts) index instead of a full scan."""
    return [m for (m,) in db.execute(
        "WITH RECURSIVE m(mic) AS (SELECT min(mic) FROM levels UNION ALL "
        "SELECT (SELECT min(mic) FROM levels WHERE mic > m.mic) FROM m WHERE mic IS NOT NULL) "
        "SELECT mic FROM m WHERE mic IS NOT NULL")]


def day_rows(db, table, t0, t1):
    """(column names, rows) of one table for the day [t0, t1)."""
    if table == "levels":   # per mic, so the (mic, ts) index is used
        cur = db.execute("SELECT * FROM levels LIMIT 0")
        rows = (r for mic in mics(db) for r in db.execute(
            "SELECT * FROM levels WHERE mic = ? AND ts >= ? AND ts < ? ORDER BY ts",
            (mic, t0, t1)))
    elif table == "logger_coverage":
        cur = db.execute("SELECT max(start_ts, ?) AS start_ts, min(end_ts, ?) AS end_ts "
                         "FROM logger_coverage WHERE end_ts >= ? AND start_ts < ? "
                         "ORDER BY start_ts", (t0, t1, t0, t1))
        rows = cur
    else:
        col = TABLES[table][0]
        cur = rows = db.execute(f"SELECT * FROM {table} WHERE {col} >= ? AND {col} < ? "
                                f"ORDER BY {col}", (t0, t1))
    return [d[0] for d in cur.description], rows


def to_csv_gz(header, rows):
    """(gzipped CSV bytes, row count). The same rows always give the same bytes."""
    out, n = io.BytesIO(), 0
    with gzip.GzipFile(filename="", mode="wb", fileobj=out, mtime=0) as gz, \
            io.TextIOWrapper(gz, encoding="utf-8", newline="") as text:
        w = csv.writer(text)
        w.writerow(header)
        for row in rows:
            w.writerow(row)
            n += 1
    return out.getvalue(), n


def first_day(db):
    """The UTC date of the oldest level or position still in the database, or None."""
    starts = [db.execute("SELECT min(ts) FROM levels WHERE mic = ?", (m,)).fetchone()[0]
              for m in mics(db)]
    starts.append(db.execute("SELECT min(ts) FROM aircraft_positions").fetchone()[0])
    starts = [s for s in starts if s is not None]
    return dt.datetime.fromtimestamp(min(starts), dt.timezone.utc).date() if starts else None


def delete_day(db, table, t0):
    """Delete one day of a big table an hour at a time, so the services never wait long."""
    for h in range(24):
        a, b = t0 + h * 3600, t0 + (h + 1) * 3600
        if table == "levels":
            for mic in mics(db):
                db.execute("DELETE FROM levels WHERE mic = ? AND ts >= ? AND ts < ?", (mic, a, b))
        else:
            db.execute(f"DELETE FROM {table} WHERE ts >= ? AND ts < ?", (a, b))
        db.commit()


def integer(v):
    """An INTEGER column's value: SQLite lets those hold decimals too, so keep any it had."""
    try:
        return int(v)
    except ValueError:
        return float(v)


def load_file(db, table, data):
    """Insert one archived file's rows. Empty fields go back in as NULL.

    Numbers are parsed here, not by SQLite, whose text-to-REAL conversion can be a bit off in
    the last digit: Python's float() gives back exactly the value that was written."""
    reader = csv.reader(io.StringIO(gzip.decompress(data).decode("utf-8")))
    header = next(reader)
    types = {name: kind for _, name, kind, *_ in db.execute(f"PRAGMA table_info({table})")}
    parse = [{"REAL": float, "INTEGER": integer}.get(types[c], str) for c in header]
    sql = (f"INSERT INTO {table} ({', '.join(header)}) "
           f"VALUES ({', '.join('?' * len(header))})")
    db.executemany(sql, ([p(v) if v != "" else None for p, v in zip(parse, row)]
                         for row in reader))


# --- What each machine runs -------------------------------------------------------------------

def upload(db, r2, now, dry_run=False):
    """Upload every finished day not archived yet."""
    done = {(d, t) for d, t in db.execute("SELECT day, tbl FROM archived")}
    day = first_day(db)
    last = dt.datetime.fromtimestamp(now - ARCHIVE_AFTER_S, dt.timezone.utc).date() \
        - dt.timedelta(days=1)
    while day and day <= last:
        todo = [t for t in TABLES if (day.isoformat(), t) not in done]
        if todo and dry_run:
            log.info("would upload %s: %s", day, ", ".join(todo))
        elif todo:
            t0, parts = day_start(day), []
            for table in todo:
                body, n = to_csv_gz(*day_rows(db, table, t0, t0 + DAY_S))
                md5 = hashlib.md5(body).hexdigest()
                r2.put(file_key(table, day), body)
                if not r2.verified(file_key(table, day), len(body), md5):
                    raise SystemExit(f"{file_key(table, day)}: R2's copy doesn't match")
                db.execute("INSERT INTO archived (day, tbl, rows, bytes, md5, uploaded_ts) "
                           "VALUES (?, ?, ?, ?, ?, ?)",
                           (day.isoformat(), table, n, len(body), md5, time.time()))
                db.commit()
                parts.append(f"{table} {n} rows {len(body) / 1e6:.1f} MB")
            log.info("uploaded %s: %s", day, ", ".join(parts))
        day += dt.timedelta(days=1)


def prune(db, r2, now, dry_run=False):
    """Delete the big tables' rows for days older than KEEP_DAYS whose files check out on R2."""
    big = [t for t, (_, deleted) in TABLES.items() if deleted]
    cutoff = now - KEEP_DAYS * DAY_S
    rows = db.execute(f"SELECT day, tbl, bytes, md5 FROM archived WHERE pruned_ts IS NULL "
                      f"AND tbl IN ({', '.join('?' * len(big))}) ORDER BY day", big).fetchall()
    for day_s, table, size, md5 in rows:
        day = dt.date.fromisoformat(day_s)
        if day_start(day) + DAY_S > cutoff:
            continue
        if dry_run:
            log.info("would delete %s %s from the Pi", day, table)
            continue
        if not r2.verified(file_key(table, day), size, md5):
            log.error("%s doesn't match R2: keeping it on the Pi", file_key(table, day))
            continue
        delete_day(db, table, day_start(day))
        db.execute("UPDATE archived SET pruned_ts = ? WHERE day = ? AND tbl = ?",
                   (time.time(), day_s, table))
        db.commit()
        log.info("deleted %s %s from the Pi (on R2)", day, table)


def merge(db, cache_dir, config_path=ARCHIVE_CONFIG):
    """Put the days a Pi database copy has deleted back into it, from the archive.

    For the Mac's analysis copy: downloads each file once into cache_dir (files never change),
    checks its MD5, then replaces that day's rows with the archived ones."""
    try:
        rows = db.execute("SELECT day, tbl, md5 FROM archived WHERE pruned_ts IS NOT NULL "
                          "ORDER BY day").fetchall()
    except sqlite3.OperationalError:   # copied from a Pi without the archive yet
        return
    if not rows:
        return
    r2 = R2(config_path)
    for day_s, table, md5 in rows:
        day = dt.date.fromisoformat(day_s)
        path = Path(cache_dir) / file_key(table, day)
        if not path.exists() or hashlib.md5(path.read_bytes()).hexdigest() != md5:
            data = r2.get(file_key(table, day))
            if hashlib.md5(data).hexdigest() != md5:
                raise SystemExit(f"{file_key(table, day)}: download doesn't match its MD5")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        delete_day(db, table, day_start(day))
        load_file(db, table, path.read_bytes())
        db.commit()
    print(f"Added {len(rows)} archived day-tables back from R2")


def restore(out, config_path=ARCHIVE_CONFIG):
    """Build a whole database from the archive alone, e.g. if the Pi's SD card dies."""
    out = Path(out)
    if out.exists():
        raise SystemExit(f"{out} already exists")
    r2, db = R2(config_path), connect(out)
    for table in TABLES:
        n = 0
        for key in r2.keys(f"{table}/"):
            load_file(db, table, r2.get(key))
            n += 1
        db.commit()
        print(f"{table}: {n} days")
    try:   # the location never leaves the house; fill meta back in from location.json if here
        lat, lon, alt_m, geoid_m = home_location()
        for k, v in [("receiver_lat", lat), ("receiver_lon", lon), ("receiver_alt_m", alt_m),
                     ("geoid_m", geoid_m)]:
            set_meta(db, k, v)
        db.commit()
    except SystemExit:
        print("No location.json here: meta is empty, so add the receiver location yourself")
    print(f"Restored to {out}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="say what would happen, only")
    parser.add_argument("--restore", type=Path, metavar="OUT.db",
                        help="rebuild a database from the archive into this new file")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.restore:
        return restore(args.restore)
    r2 = None if args.dry_run else R2()   # a dry run never touches the network
    db, now = connect(), time.time()
    try:
        upload(db, r2, now, args.dry_run)
        prune(db, r2, now, args.dry_run)
    except (urllib.error.URLError, OSError) as e:   # nothing is deleted unless checked on R2
        log.error("stopped, will try again next run: %s", e)
        sys.exit(1)
    on_r2 = db.execute("SELECT count(DISTINCT day), coalesce(sum(bytes), 0) FROM archived")
    days, size = on_r2.fetchone()
    log.info("archive: %d days, %.0f MB on R2; Pi: database %.0f MB, %.0f GB free",
             days, size / 1e6, sum(p.stat().st_size for p in DATA_DIR.glob("noise.db*")) / 1e6,
             shutil.disk_usage(DATA_DIR).free / 1e9)


if __name__ == "__main__":
    main()
