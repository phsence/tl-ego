# -*- coding: utf-8 -*-
"""Windowed recovery v2 for ego's grid_edges.sqlite (26GB, malformed + HANGS on read).

Why v2 (both bugs measured on the live DB, both inherited from the skill script):
  1. Digit split drops the band below `lo+'0'`:  keys 'n-...' live in ['n','n0')
     and that band was NEVER copied -> silent loss (no error, no counter).
  2. Splitting ['n','n0') produced a child equal to the parent -> recursion to
     MAX_DEPTH, then degrade spun forever: reads here HANG (burn CPU, never raise),
     the in-process progress handler does not get control from the pager loop.
  => v2: byte-wise interval bisection (lossless, always shrinks) and EVERY window
     copy runs in its own child process with a hard timeout (a hang costs <=TIMEOUT,
     the parent bisects and moves on). Loss is reported, never silent.

Schema facts: edges + hebbian_active_edges are PK(src,dst); edges is WITHOUT ROWID
(no rowid -> windowing is mandatory). idx_src/idx_dst are rebuilt after the copy
because the app re-creates them at open (a missing index would stall ego's boot).

Usage:  python _db_rebuild.py              # parent: copies + bisects + rebuilds idx
        python _db_rebuild.py --window T LO HI   # child: copy one window, print ROWS
"""
import os
import sqlite3
import subprocess
import sys
import time

DATA = r'C:\Users\Administrator\.openclaw\skills\neural_grid\data'
SRC = os.path.join(DATA, 'grid_edges.sqlite')
DST = os.path.join(DATA, 'grid_edges.rebuilt.sqlite')
TABLES = [('edges', 'src, dst, weight, last_update'),
          ('hebbian_active_edges', 'src, dst, weight, last_update')]
KEY_COL = 'src'
WINDOW_TIMEOUT_S = 25
MAX_DEPTH = 40
TOP = [('', '0')] + [(chr(c), chr(c + 1)) for c in range(ord('0'), ord('~'))] + [('\x7f', '\U0010ffff')]
ARGS_MARK = '--window'


def log(msg):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


# ---------------------------------------------------------------- child mode
def child_copy(table, cols, lo, hi):
    src = sqlite3.connect('file:%s?mode=ro' % SRC.replace('\\', '/'), uri=True, timeout=20)
    dst = sqlite3.connect(DST, timeout=20)
    dst.execute('PRAGMA journal_mode=WAL')
    dst.execute('PRAGMA synchronous=NORMAL')
    marks = ','.join('?' * len(cols.split(',')))
    sql = 'SELECT %s FROM %s WHERE %s >= ? AND %s < ?' % (cols, table, KEY_COL, KEY_COL)
    n = 0
    try:
        cur = src.execute(sql, (lo, hi))
        while True:
            rows = cur.fetchmany(20000)
            if not rows:
                break
            dst.executemany('INSERT OR IGNORE INTO %s (%s) VALUES (%s)' % (table, cols, marks), rows)
            n += len(rows)
        dst.commit()
        print('ROWS %d' % n, flush=True)
        return 0
    except sqlite3.DatabaseError as e:
        print('CORRUPT %s' % str(e)[:90], flush=True)
        return 3


# --------------------------------------------------------------- parent mode
def bisect(lo, hi):
    """Split [lo,hi) into two strictly smaller intervals (lossless). Bisects the
    first differing char, or (when lo is a prefix of hi) the next char of hi."""
    n = min(len(lo), len(hi))
    i = 0
    while i < n and lo[i] == hi[i]:
        i += 1
    if i >= n:                                   # lo is a prefix of hi: 'n' vs 'n0'
        a = ord(hi[i]) if i < len(hi) else 0
        if a <= 1:
            return []
        mid = lo + chr(a // 2) + '\U0010ffff'
        return [(lo, mid), (mid, hi)]
    a, b = ord(lo[i]), ord(hi[i])
    if b - a <= 1:
        # Adjacent chars ('n' vs 'o', '9' vs ':', 'n-a' vs 'n-b'): bisecting the
        # char has no room, so re-anchor ABOVE lo (append a char < hi[0]'s byte).
        if b <= 1:
            return []
        mid = lo + chr(b // 2) + '􏿿'
        if not (lo < mid < hi):
            return []
        return [(lo, mid), (mid, hi)]
    m = chr((a + b) // 2)
    return [(lo, lo[:i] + m), (lo[:i] + m, hi)]


class Stat:
    def __init__(self):
        self.rows = 0
        self.wins = 0
        self.hung = 0
        self.corrupt = 0
        self.lost = []


def run_child(table, cols, lo, hi):
    cmd = [sys.executable, os.path.abspath(__file__), ARGS_MARK, table, lo.encode('utf-8').hex(), hi.encode('utf-8').hex()]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=WINDOW_TIMEOUT_S, text=True, encoding='utf-8', errors='replace')
    except subprocess.TimeoutExpired:
        return 'HANG', 0
    out = (p.stdout or '').strip().splitlines()
    last = out[-1] if out else ''
    if last.startswith('ROWS '):
        return 'OK', int(last.split()[1])
    return 'CORRUPT', 0


def copy(parent, table, cols, lo, hi, depth, st):
    if lo >= hi:
        return
    if depth > MAX_DEPTH:
        st.lost.append((lo, hi))
        log('  LOST (depth) %r..%r' % (lo[:26], hi[:26]))
        return
    verdict, rows = run_child(table, cols, lo, hi)
    if verdict == 'OK':
        st.rows += rows
        st.wins += 1
        if rows:
            log('  win %r..%r d=%d -> %d rows (total %d)' % (lo[:24], hi[:24], depth, rows, st.rows))
        return
    st.hung += (verdict == 'HANG')
    st.corrupt += (verdict == 'CORRUPT')
    kids = bisect(lo, hi)
    log('  %s %r..%r d=%d -> bisect into %d' % (verdict, lo[:26], hi[:26], depth, len(kids)))
    if not kids:
        st.lost.append((lo, hi))
        log('  LOST (atomic) %r..%r' % (lo[:26], hi[:26]))
        return
    for a, b in kids:
        copy(parent, table, cols, a, b, depth + 1, st)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == ARGS_MARK:
        table = sys.argv[2]
        lo = bytes.fromhex(sys.argv[3]).decode('utf-8')
        hi = bytes.fromhex(sys.argv[4]).decode('utf-8')
        return child_copy(table, dict(TABLES)[table], lo, hi)

    t0 = time.time()
    src = sqlite3.connect('file:%s?mode=ro' % SRC.replace('\\', '/'), uri=True, timeout=30)
    dst = sqlite3.connect(DST, timeout=30)          # NOT removed: INSERT OR IGNORE dedups
    dst.execute('PRAGMA journal_mode=WAL')
    dst.commit()
    for table, _ in TABLES:
        ddl = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if ddl:
            dst.execute('CREATE TABLE IF NOT EXISTS %s' % ddl[0].replace('CREATE TABLE ', '', 1))
    dst.commit()
    st = Stat()
    for table, cols in TABLES:
        log('copying %s (resume-safe) ...' % table)
        before = st.rows
        for lo, hi in TOP:
            copy(None, table, cols, lo, hi, 0, st)
        log('%s done: +rows=%d hung=%d corrupt=%d lost=%d (%.0fs)' % (
            table, st.rows - before, st.hung, st.corrupt, len(st.lost), time.time() - t0))

    log('building indexes ...')
    for sql in ('CREATE INDEX IF NOT EXISTS idx_src ON edges(src)',
                'CREATE INDEX IF NOT EXISTS idx_dst ON edges(dst)'):
        dst.execute(sql)
        dst.commit()
        log('  %s (%.0fs)' % (sql.split()[5], time.time() - t0))

    log('verify ...')
    bad = [r[0] for r in dst.execute('PRAGMA quick_check') if r[0] != 'ok']
    for table, _ in TABLES:
        log('  %s rows=%d' % (table, dst.execute('SELECT count(*) FROM %s' % table).fetchone()[0]))
    for lo, hi in st.lost[:12]:
        log('  LOST %r..%r' % (lo[:40], hi[:40]))
    log('integrity: %s | rows=%d wins=%d hung=%d corrupt=%d lost=%d | elapsed=%.0fs' % (
        'OK' if not bad else 'FAILED %s' % bad[:2], st.rows, st.wins, st.hung, st.corrupt, len(st.lost), time.time() - t0))
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
