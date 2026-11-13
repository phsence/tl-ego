# -*- coding: utf-8 -*-
"""Windowed recovery for ego's grid_edges.sqlite (26GB, partially malformed).

Adapted from skill sqlite-corruption-recovery scripts/recover_sqlite_windowed.py
for THIS schema:
  edges                WITHOUT ROWID, PK(src,dst)   -> MUST window by key (no rowid)
  hebbian_active_edges rowid table,   PK(src,dst)
  idx_src / idx_dst    rebuilt after the copy (the app recreates them at open,
                       so the target must already carry them or ego stalls).

Reads the live DB read-only (safe while the ego runs); the swap happens later,
with the service stopped. Scratch probe file _probe_win.sqlite is used only to
isolate which windows are corrupt (maps a bad window to a key prefix).

RULES (from the skill, each cost hours when violated):
  * WAL on the target, never journal_mode=OFF (killed run would corrupt it).
  * Never one big streamed SELECT (cursor dies silently at the first bad page).
  * MAX_DEPTH 9: deeper splitting is ~10x windows per level for no gain.
"""
import os
import sqlite3
import sys
import time

DATA = r'C:\Users\Administrator\.openclaw\skills\neural_grid\data'
SRC = os.path.join(DATA, 'grid_edges.sqlite')
DST = os.path.join(DATA, 'grid_edges.rebuilt.sqlite')
KEY_COL = 'src'
TABLES = [
    ('edges', 'src, dst, weight, last_update'),
    ('hebbian_active_edges', 'src, dst, weight, last_update'),
]
MAX_DEPTH = 9
COMMIT_EVERY = 10
STALL_S = 30            # a read with no progress this long gets interrupted: this DB HANGS
                       # (quick_check and the big 'n' window both wedged forever, no error raised)
_progress = [0.0]      # touched by every successful fetch; read by the progress handler


def _stall_guard():
    return 1 if (time.time() - _progress[0]) > STALL_S else 0


def log(msg):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


class Rec:
    def __init__(self):
        self.ok = 0
        self.corrupt = 0
        self.lost = 0
        self.skipped = 0


def _ins(dst_cur, table, cols, rows):
    marks = ','.join('?' * len(cols.split(',')))
    dst_cur.executemany('INSERT OR IGNORE INTO %s (%s) VALUES (%s)' % (table, cols, marks), rows)


def copy_window(cur, dst_cur, dst_con, rec, table, cols, lo, hi, depth):
    if lo >= hi or depth > MAX_DEPTH:
        return
    try:
        cur.execute('SELECT %s FROM %s WHERE %s >= ? AND %s < ?' % (cols, table, KEY_COL, KEY_COL), (lo, hi))
        _progress[0] = time.time()
        n = 0
        while True:
            rows = cur.fetchmany(20000)
            _progress[0] = time.time()
            if not rows:
                break
            _ins(dst_cur, table, cols, rows)
            n += len(rows)
        rec.ok += n
        if n:
            log('  win[%s] %r..%r depth=%d -> %d rows (ok=%d)' % (table, lo[:22], hi[:22], depth, n, rec.ok))
        return
    except sqlite3.DatabaseError as e:
        rec.corrupt += 1
        log('  CORRUPT win[%s] %r..%r depth=%d: %s' % (table, lo[:24], hi[:24], depth, str(e)[:80]))
        if depth >= MAX_DEPTH:
            degrade(cur, dst_cur, rec, table, cols, lo, hi)
            return
        for d in range(10):
            wlo = lo + str(d)
            if wlo >= hi:
                break
            whi = lo + str(d + 1) if d < 9 else lo + '\U0010ffff'
            if whi > hi:
                whi = hi
            copy_window(cur, dst_cur, dst_con, rec, table, cols, wlo, whi, depth + 1)
            dst_con.commit()


def degrade(cur, dst_cur, rec, table, cols, lo, hi):
    """Depth limit reached: single rows. A corrupt page still ends the cursor
    (fetchone -> None) - same loss as sqlite's .recover, reported in rec.lost."""
    get_one = 'SELECT %s FROM %s WHERE %s >= ? AND %s < ? ORDER BY %s LIMIT 1' % (
        cols, table, KEY_COL, KEY_COL, KEY_COL)
    steps = 0
    while steps < 50000:
        try:
            row = cur.execute(get_one, (lo, hi)).fetchone()
            _progress[0] = time.time()
        except sqlite3.DatabaseError:
            rec.lost += 1
            log('  LOST window %r..%r (unreadable)' % (lo[:24], hi[:24]))
            return
        if row is None:
            return
        try:
            _ins(dst_cur, table, cols, [row])
            rec.ok += 1
        except sqlite3.DatabaseError:
            rec.skipped += 1
        lo = row[0] + '\x00'          # advance past the copied key
        steps += 1
    log('  WARN degrade window %r..%r hit 50000-row cap' % (lo[:24], hi[:24]))


def main():
    t0 = time.time()
    if os.path.exists(DST):
        os.remove(DST)
    src = sqlite3.connect('file:%s?mode=ro' % SRC.replace('\\', '/'), uri=True, timeout=60)
    src.set_progress_handler(_stall_guard, 20000)
    cur = src.cursor()
    dst = sqlite3.connect(DST, timeout=60)
    dst.execute('PRAGMA journal_mode=WAL')      # kill-safe; never OFF
    dst.execute('PRAGMA synchronous=NORMAL')
    dst_cur = dst.cursor()
    rec = Rec()

    # windows: '0'..'~' by lead char, plus catch-alls below '0' and above '~'
    # so no key can be silently skipped (honest accounting).
    prefixes = [('', '0')] + [(chr(c), chr(c + 1)) for c in range(ord('0'), ord('~'))] + [('\x7f', '\U0010ffff')]

    for table, cols in TABLES:
        ddl = cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if ddl:
            dst_cur.execute(ddl[0])
            dst.commit()
        log('copying %s ...' % table)
        before = (rec.ok, rec.corrupt, rec.lost)
        for i, (lo, hi) in enumerate(prefixes):
            copy_window(cur, dst_cur, dst, rec, table, cols, lo, hi, 0)
            if i % COMMIT_EVERY == 0:
                dst.commit()
                log('  ... %s progress ok=%d corrupt=%d lost=%d (win %d/%d)' % (
                    table, rec.ok, rec.corrupt, rec.lost, i + 1, len(prefixes)))
        dst.commit()
        log('%s done: +ok=%d +corrupt=%d +lost=%d' % (
            table, rec.ok - before[0], rec.corrupt - before[1], rec.lost - before[2]))

    log('building indexes ...')
    for idx_sql in ('CREATE INDEX IF NOT EXISTS idx_src ON edges(src)',
                    'CREATE INDEX IF NOT EXISTS idx_dst ON edges(dst)'):
        dst_cur.execute(idx_sql)
        dst.commit()
        log('  %s ok (%.0fs)' % (idx_sql.split()[5], time.time() - t0))

    log('verify ...')
    bad = [r[0] for r in dst_cur.execute('PRAGMA quick_check') if r[0] != 'ok']
    for table, _ in TABLES:
        log('  %s rows=%d' % (table, dst_cur.execute('SELECT count(*) FROM %s' % table).fetchone()[0]))
    log('integrity: %s | ok=%d corrupt=%d lost=%d skipped=%d | elapsed=%.0fs' % (
        'OK' if not bad else 'FAILED %s' % bad[:3], rec.ok, rec.corrupt, rec.lost, rec.skipped, time.time() - t0))
    src.close()
    dst.close()
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
