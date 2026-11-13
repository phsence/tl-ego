# -*- coding: utf-8 -*-
"""Sweep-based recovery v3 for ego's grid_edges.sqlite (26GB, malformed + HANGS on read).

v1/v2 lessons (all measured live):
  * reads here HANG (burn CPU in the pager loop, never raise) -> any in-process
    guard/recursion wedges. Only an out-of-process kill bounds an attempt.
  * digit-split recursion drops / re-visits intervals and can spin for hours in
    empty key space. Throw it away.
v3 algorithm: alternating forward/backward sweeps, each a child process with a
hard timeout, each COMMITTING EVERY BATCH:
    pass_fwd(lo, hi) streams rows up to the first bad page, commits, gets killed
                     or errors -> everything before the bad page is already saved
    pass_bwd(lo, hi) does the same from the top down
    parent re-seeds past the last saved key (A -> A+'\x00' / hi -> min saved above)
    until a full cycle adds zero rows. Loss = cells inside bad pages only, and the
    boundary keys are printed (honest accounting, same loss class as sqlite .recover).

Schema: edges WITHOUT ROWID PK(src,dst) (windowing mandatory); hebbian_active_edges
rowid PK(src,dst). idx_src/idx_dst rebuilt at the end (the app re-creates them at
open, so a missing index would stall ego's boot).

Usage:  python _db_rebuild3.py                       # parent sweep + verify
        python _db_rebuild3.py --pass T LO HI fwd|bwd  # child (LO/HI utf-8 hex)
"""
import os
import sqlite3
import subprocess
import sys
import time

DATA = r'C:\Users\Administrator\.openclaw\skills\neural_grid\data'
SRC = os.path.join(DATA, 'grid_edges.sqlite')
# 2026-11-02: C: 只剩 20GB，装不下 26GB 新库 -> 目标改到 H:（304GB 空闲）。
# VACUUM INTO 在同一份源上跑到 ~20GB 后报 malformed（主库确有坏页），故走本 sweep。
DST = r'H:\grid_rebuild\grid_edges.rebuilt.sqlite'
TABLES = [('edges', 'src, dst, weight, last_update'),
          ('hebbian_active_edges', 'src, dst, weight, last_update')]
KEY = 'src'
PASS_TIMEOUT_S = 150
LO0, HI0 = '', '\U0010ffff'
MARK = '--pass'


def log(msg):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), msg), flush=True)


def ro(path):
    con = sqlite3.connect('file:%s?mode=ro' % path.replace('\\', '/'), uri=True, timeout=20)
    con.execute('PRAGMA busy_timeout=20000')
    return con


# ---------------------------------------------------------------- child mode
def child_pass(table, cols, lo, hi, order):
    src = ro(SRC)
    dst = sqlite3.connect(DST, timeout=30)
    dst.execute('PRAGMA journal_mode=WAL')
    dst.execute('PRAGMA synchronous=NORMAL')
    marks = ','.join('?' * len(cols.split(',')))
    ins = 'INSERT OR IGNORE INTO %s (%s) VALUES (%s)' % (table, cols, marks)
    sql = 'SELECT %s FROM %s WHERE %s >= ? AND %s < ? ORDER BY %s %s' % (
        cols, table, KEY, KEY, KEY, order)
    n = 0
    try:
        cur = src.execute(sql, (lo, hi))
        while True:
            rows = cur.fetchmany(20000)
            if not rows:
                break
            dst.executemany(ins, rows)
            dst.commit()                      # committed per batch: a kill keeps progress
            n += len(rows)
        print('PASS-OK %d' % n, flush=True)
        return 0
    except sqlite3.DatabaseError as e:
        print('PASS-CORRUPT %d %s' % (n, str(e)[:80]), flush=True)
        return 3


# --------------------------------------------------------------- parent mode
def run_pass(table, cols, lo, hi, order):
    cmd = [sys.executable, os.path.abspath(__file__), MARK, table,
           lo.encode('utf-8').hex(), hi.encode('utf-8').hex(), order]
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=PASS_TIMEOUT_S,
                           text=True, encoding='utf-8', errors='replace')
    except subprocess.TimeoutExpired:
        return 'KILLED', -1
    tail = (p.stdout or '').strip().splitlines()
    last = tail[-1] if tail else ''
    if last.startswith('PASS-OK'):
        return ('OK', int(last.split()[1]))
    if last.startswith('PASS-CORRUPT'):
        return ('CORRUPT', int(last.split()[1]))
    return ('ERR', -1)


def dst_count(con, table):
    return con.execute('SELECT count(*) FROM %s' % table).fetchone()[0]


def sweep(table, cols):
    dstc = sqlite3.connect(DST, timeout=30)
    lo, hi = LO0, HI0
    fwd_done = bwd_done = False
    rounds = 0
    hangs = []
    t0 = time.time()
    while not (fwd_done and bwd_done) and rounds < 200:
        rounds += 1
        if not fwd_done:
            before = dst_count(dstc, table)
            verdict, n = run_pass(table, cols, lo, hi, 'ASC')
            added = dst_count(dstc, table) - before
            log('  fwd[%s] %r..%r -> %s(+%d rows, total %d)' % (
                table, lo[:26], hi[:26], verdict, added, dst_count(dstc, table)))
            if verdict == 'OK':
                fwd_done = True
            elif added == 0:
                fwd_done = True                      # stuck with nothing new: front is done
            else:
                last = dstc.execute('SELECT max(%s) FROM %s WHERE %s >= ? AND %s < ?' % (KEY, table, KEY, KEY), (lo, hi)).fetchone()[0]
                hangs.append(('fwd', last, None))
                lo = last + '\x00'                   # smallest string > last
        if not bwd_done:
            before = dst_count(dstc, table)
            verdict, n = run_pass(table, cols, lo, hi, 'DESC')
            added = dst_count(dstc, table) - before
            log('  bwd[%s] %r..%r -> %s(+%d rows, total %d)' % (
                table, lo[:26], hi[:26], verdict, added, dst_count(dstc, table)))
            if verdict == 'OK':
                bwd_done = True
            elif added == 0:
                bwd_done = True
            else:
                first = dstc.execute('SELECT min(%s) FROM %s WHERE %s >= ? AND %s < ?' % (KEY, table, KEY, KEY), (lo, hi)).fetchone()[0]
                hangs.append(('bwd', None, first))
                hi = first
        if rounds % 6 == 0:
            log('  ... %s round %d rows=%d (%.0fs)' % (table, rounds, dst_count(dstc, table), time.time() - t0))
    log('%s sweep done: rows=%d rounds=%d (%.0fs)' % (table, dst_count(dstc, table), rounds, time.time() - t0))
    for d, a, b in hangs[:10]:
        log('  boundary after %s stop: %r | %r' % (d, (a or '')[:60], (b or '')[:60]))
    dstc.close()
    return hangs


def main():
    if len(sys.argv) > 1 and sys.argv[1] == MARK:
        table = sys.argv[2]
        lo = bytes.fromhex(sys.argv[3]).decode('utf-8')
        hi = bytes.fromhex(sys.argv[4]).decode('utf-8')
        order = 'DESC' if sys.argv[5].upper() == 'DESC' else 'ASC'
        return child_pass(table, dict(TABLES)[table], lo, hi, order)

    t0 = time.time()
    src = ro(SRC)
    dst = sqlite3.connect(DST, timeout=30)
    dst.execute('PRAGMA journal_mode=WAL')
    for table, _ in TABLES:
        ddl = src.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        if ddl:
            dst.execute(ddl[0].replace('CREATE TABLE', 'CREATE TABLE IF NOT EXISTS', 1))
    dst.commit()
    for table, cols in TABLES:
        log('sweeping %s ...' % table)
        sweep(table, cols)

    log('building indexes ...')
    for sql in ('CREATE INDEX IF NOT EXISTS idx_src ON edges(src)',
                'CREATE INDEX IF NOT EXISTS idx_dst ON edges(dst)'):
        dst.execute(sql)
        dst.commit()
        log('  %s (%.0fs)' % (sql.split()[5], time.time() - t0))

    log('verify ...')
    bad = [r[0] for r in dst.execute('PRAGMA quick_check') if r[0] != 'ok']
    for table, _ in TABLES:
        log('  %s rows=%d' % (table, dst_count(dst, table)))
    log('integrity: %s | elapsed=%.0fs' % ('OK' if not bad else 'FAILED %s' % bad[:2], time.time() - t0))
    return 0 if not bad else 1


if __name__ == '__main__':
    sys.exit(main())
