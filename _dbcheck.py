"""Read-only quick_check probe for the 26GB grid_edges.sqlite (Step 0 of the skill).
Writes nothing. Prints quick_check(100) rows so a corrupt page maps to a tree via
sqlite_master.rootpage (see skill sqlite-corruption-recovery)."""
import sqlite3
import time

P = r'C:\Users\Administrator\.openclaw\skills\neural_grid\data\grid_edges.sqlite'
t0 = time.time()
uri = 'file:' + P.replace('\\', '/') + '?mode=ro'
con = sqlite3.connect(uri, uri=True, timeout=300)
try:
    for row in con.execute('PRAGMA quick_check(100)'):
        print('CHECK:', row[0], flush=True)
except Exception as e:
    print('CHECK-ERR:', type(e).__name__, str(e)[:300], flush=True)
try:
    print('SCHEMA:', flush=True)
    for name, typ, root in con.execute("SELECT name, type, rootpage FROM sqlite_master ORDER BY rootpage"):
        print('  %-28s %-6s rootpage=%s' % (name, typ, root), flush=True)
    print('max_rowid probe...', flush=True)
    print('  max(rowid) =', con.execute('SELECT max(rowid) FROM edges').fetchone()[0], flush=True)
except Exception as e:
    print('SCHEMA-ERR:', type(e).__name__, str(e)[:200], flush=True)
print('done in %.1fs' % (time.time() - t0), flush=True)
