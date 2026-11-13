"""One runnable check: EdgeStoreV2.get_edges_topk must equal a full-read top-k.

Run:  python test_edges_topk.py
Fails loudly if the SQL cut changes which edges propagation sees.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import edge_store_v2 as esv  # noqa: E402

K = 200


def main():
    st = esv.EdgeStoreV2()
    conn = st._get_sqlite_conn()
    assert conn is not None, "grid DB not resolvable — check _resolve_grid_db candidates"
    srcs = [r[0] for r in conn.execute(
        "select src, count(*) c from edges group by src order by c desc limit 50")]
    assert srcs, "no sources in edges table"

    t0 = time.time()
    full = [sorted(st._sqlite_edges([s]).get(s, []), key=lambda x: -x[1])[:K] for s in srcs]
    t_full = time.time() - t0

    t0 = time.time()
    top = [st.get_edges_topk(s, K) for s in srcs]
    t_top = time.time() - t0

    for s, f, tp in zip(srcs, full, top):
        assert {d: w for d, w in f} == {d: w for d, w in tp}, \
            f"{s}: full-read top{K} != get_edges_topk ({len(f)} vs {len(tp)})"
        ws = [w for _, w in tp]
        assert ws == sorted(ws, reverse=True), f"{s}: not weight-descending"
        assert len(tp) <= K, f"{s}: returned {len(tp)} > {K}"

    print(f"OK {len(srcs)} sources identical; full {t_full:.2f}s -> topk {t_top:.2f}s "
          f"(x{t_full / max(t_top, 1e-9):.1f} faster)")


if __name__ == "__main__":
    main()
