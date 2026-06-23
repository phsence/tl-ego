import sqlite3
db = r"C:\Users\Administrator\.openclaw\workspace\neural_grid_data_100k_backup\grid_edges.sqlite"
c = sqlite3.connect(db)
r = c.execute("SELECT COUNT(*), ROUND(AVG(weight),4) FROM edges WHERE last_update > datetime('now', '-1 minute')").fetchone()
print(f"Edges updated in last 1min: {r[0]}, avg_weight: {r[1]}")
r2 = c.execute("SELECT src,dst,weight,last_update FROM edges WHERE last_update > datetime('now', '-1 minute') LIMIT 5").fetchall()
for s,d,w,lu in r2:
    print(f"  {s} -> {d} : {w} @ {lu}")
c.close()
