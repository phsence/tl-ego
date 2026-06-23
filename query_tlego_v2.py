"""Query TL-Ego memory from Hy-Memory and graph-memory."""
import requests, json, sys, sqlite3
from pathlib import Path
from datetime import datetime, timezone

BASE = 'http://127.0.0.1:19527'

def print_header(label):
    print()
    print('=' * 70)
    print(f'  {label}')
    print('=' * 70)

# === 1. Hy-Memory: TL-Ego memories ===
print_header('Hy-Memory: TL-Ego contemplation memories')
r = requests.post(BASE + '/api/v1/search', json={
    'query': 'contemplation mental state agree disagree meditation self-awareness',
    'user_ids': ['tl-ego'],
    'limit': 30
}, timeout=15)

if r.status_code == 200:
    results = r.json().get('results', [])
    print(f'Found {len(results)} entries')
    for i, item in enumerate(results[:25]):
        content = item.get('content', '')[:400]
        mid = item.get('memory_id', '?')
        layer = item.get('layer', '?')
        created = item.get('created_at', '?')
        uid = item.get('user_id', '?')
        aid = item.get('agent_id', '?')

        # Detect agree/disagree verdict
        cl = content.lower()
        verdict = ''
        if 'agree' in cl and 'disagree' not in cl:
            verdict = '[AGREE]'
        elif 'disagree' in cl:
            verdict = '[DISAGREE]'

        print(f'\n--- Entry {i+1} (mid={mid}) {verdict} ---')
        print(f'  user={uid} agent={aid} layer={layer} created={created}')
        print(f'  content: {content}')
else:
    print(f'Error: {r.status_code} {r.text[:500]}')

# === 2. Graph-memory: recent contemplation nodes ===
print_header('graph-memory: recent EVENT nodes')
gm_db = Path.home() / '.openclaw' / 'graph-memory.db'
conn = sqlite3.connect(str(gm_db))
cur = conn.execute('''
    SELECT id, name, type, description, content, created_at
    FROM gm_nodes
    WHERE type = 'EVENT'
    ORDER BY id DESC
    LIMIT 40
''')
count = 0
for row in cur.fetchall():
    count += 1
    node_id = row[0]
    name = row[1] or '(no name)'
    desc = (row[3] or '')[:150]
    content = (row[4] or '')[:250]
    created = row[5] or '?'
    print(f'\n  n-{node_id}: name={name}')
    print(f'    desc: {desc}')
    if content:
        print(f'    content: {content}')
    print(f'    created: {created}')

print(f'\n  Total EVENT nodes in this batch: {count}')

# Count total
cur2 = conn.execute('SELECT COUNT(*) FROM gm_nodes')
total = cur2.fetchone()[0]
print(f'  Total nodes in graph-memory: {total}')
conn.close()
