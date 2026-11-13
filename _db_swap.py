# -*- coding: utf-8 -*-
"""Final swap: grid_edges.sqlite (corrupt) -> recovered copy, keeping the original.

Sequence (each step verified; nothing deleted):
  1. verify DST (quick_check ok) + fold its WAL
  2. stop the ego tree: listener pid -> parent launcher -> the bat's cmd, then
     confirm port 8766 is free AND no tl_ego_server.py python remains
  3. MOVE the corrupt db + its wal/shm off-volume to H:\\ (off-volume rollback
     copy AND frees 26GB on C: — C: was at 4.8GB free after idx_src)
  4. move the recovered file into the live path (checkpointed first)
  5. build idx_dst on the live path (space now available; the app's own
     CREATE INDEX IF NOT EXISTS would otherwise stall its boot with no room)
  6. start the ego via run_tlego_service.bat, wait for the listener
  7. prove persistence: manual INSERT+DELETE probe on the live path while the
     service runs; print the listener so a double instance is caught by eye too.
"""
import os
import shutil
import sqlite3
import subprocess
import sys
import time

DATA = r'C:\Users\Administrator\.openclaw\skills\neural_grid\data'
LIVE = os.path.join(DATA, 'grid_edges.sqlite')
NEW = os.path.join(DATA, 'grid_edges.rebuilt.sqlite')
HBAK = r'H:\grid_edges.corrupt.bak-20260930.sqlite'
WS = r'C:\Users\Administrator\.openclaw\workspace\projects\tl-ego'


def log(m):
    print('[%s] %s' % (time.strftime('%H:%M:%S'), m), flush=True)


def ps(cmd):
    return subprocess.run(['powershell', '-NoProfile', '-Command', cmd],
                          capture_output=True, text=True, errors='replace').stdout.strip()


def listeners():
    out = subprocess.run(['netstat', '-ano'], capture_output=True, text=True, errors='replace').stdout
    return [ln.split()[-1] for ln in out.splitlines() if ':8766' in ln and 'LISTENING' in ln.upper()]


def ego_tree_pids():
    """listener + ancestors whose cmdline is ego's own (launcher python / the bat's cmd)."""
    pids = set()
    for pid in listeners():
        chain = [pid]
        for _ in range(4):
            ppid = ps('(Get-CimInstance Win32_Process -Filter "ProcessId=%s").ParentProcessId' % chain[-1])
            if not ppid.isdigit() or ppid == '0':
                break
            chain.append(ppid)
        for p in chain:
            cmd = ps('(Get-CimInstance Win32_Process -Filter "ProcessId=%s").CommandLine' % p)
            if p == pid or 'tl_ego_server' in cmd or 'run_tlego_service' in cmd:
                pids.add(p)
    return sorted(pids)


def stop_ego():
    for _ in range(3):
        pids = ego_tree_pids()
        log('  ego tree: %s' % pids)
        for p in pids:
            ps('Stop-Process -Id %s -Force -EA SilentlyContinue' % p)
        time.sleep(3)
        n = ps("(Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Where-Object { $_.CommandLine -like '*tl_ego_server*' }).Count").strip()
        if not listeners() and n in ('', '0'):
            return True
    return False


def main():
    # 1 (idempotent: a re-run after a partial swap continues from where it stopped)
    if os.path.exists(NEW):
        con = sqlite3.connect(NEW, timeout=120)
        bad = [r[0] for r in con.execute('PRAGMA quick_check') if r[0] != 'ok']
        assert not bad, 'recovered file quick_check: %s' % bad[:2]
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        rows = con.execute('SELECT count(*) FROM edges').fetchone()[0]
        con.close()
        log('recovered file OK: edges=%d' % rows)
    else:
        log('NEW already placed (LIVE exists=%s, H: bak exists=%s) — continuing' % (
            os.path.exists(LIVE), os.path.exists(HBAK)))

    # 2
    assert stop_ego(), 'could not stop the ego cleanly'
    log('ego stopped, port 8766 free')

    # 3
    for suffix in ('', '-wal', '-shm'):
        src = LIVE + suffix
        if os.path.exists(src):
            log('  moving %s -> %s (%.0f MB)' % (os.path.basename(src), os.path.basename(HBAK + suffix),
                                                 os.path.getsize(src) / 1e6))
            t = time.time()
            shutil.move(src, HBAK + suffix)
            log('    done in %.0fs' % (time.time() - t))

    # 4 (a watchdog may have respawned the ego and created a stub LIVE -> it holds
    #    the file open, so kill it again and retry before replacing)
    for attempt in range(6):
        try:
            for suffix in ('', '-wal', '-shm'):
                if os.path.exists(NEW + suffix):
                    os.replace(NEW + suffix, LIVE + suffix)
            break
        except PermissionError as e:
            log('  replace blocked (attempt %d): %s -> kill ego again' % (attempt + 1, e))
            stop_ego()
            time.sleep(4)
    assert os.path.exists(LIVE) and not os.path.exists(NEW), 'replace never succeeded'
    log('recovered file in place: %s' % LIVE)
    log('  free on C: %s' % (ps("(Get-PSDrive C).Free/1GB") or '?'))

    # 5
    lc = sqlite3.connect(LIVE, timeout=600)
    have = [r[0] for r in lc.execute("SELECT name FROM sqlite_master WHERE type='index'")]
    if 'idx_dst' not in have:
        t = time.time()
        lc.execute('CREATE INDEX IF NOT EXISTS idx_dst ON edges(dst)')
        lc.commit()
        log('  idx_dst built (%.0fs)' % (time.time() - t))
    if 'idx_src' not in have:
        t = time.time()
        lc.execute('CREATE INDEX IF NOT EXISTS idx_src ON edges(src)')
        lc.commit()
        log('  idx_src built (%.0fs)' % (time.time() - t))
    lc.close()

    # 6
    subprocess.Popen('cmd /c run_tlego_service.bat', cwd=WS, shell=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log('ego starting ...')
    for _ in range(90):
        time.sleep(5)
        if listeners():
            break
    log('listeners on 8766: %s' % listeners())

    # 7 — persistence probe against the LIVE path while the service runs
    time.sleep(20)
    probe_src, probe_dst = '__swap_probe__', '__swap_probe__'
    pc = sqlite3.connect(LIVE, timeout=30)
    try:
        pc.execute('INSERT OR REPLACE INTO edges (src, dst, weight, last_update) VALUES (?,?,?,?)',
                   (probe_src, probe_dst, 0.5, time.strftime('%Y-%m-%dT%H:%M:%S')))
        pc.commit()
        got = pc.execute('SELECT weight FROM edges WHERE src=? AND dst=?', (probe_src, probe_dst)).fetchone()
        pc.execute('DELETE FROM edges WHERE src=? AND dst=?', (probe_src, probe_dst))
        pc.commit()
        log('write probe OK (round-trip %s)' % got)
    except sqlite3.DatabaseError as e:
        log('write probe FAILED: %s' % e)
        return 2
    finally:
        pc.close()
    log('swap complete')
    return 0


if __name__ == '__main__':
    sys.exit(main())
