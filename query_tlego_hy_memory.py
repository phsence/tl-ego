"""Query Hy-Memory for TL-Ego's recent contemplation history."""
import requests, json, sys
from datetime import datetime, timezone

BASE = 'http://127.0.0.1:13199'

# 1. Search TL-Ego's memories - general consciousness/search
r = requests.post(f'{BASE}/api/v1/search', json={
    'query': 'TL-Ego meditation consciousness self-awareness mental state agree disagree',
    'user_ids': ['tl-ego'],
    'limit': 20
}, timeout=15)
print(f'=== Search: mental state / agree/disagree ===')
if r.status_code == 200:
    data = r.json()
    results = data.get('results', [])
    print(f'Found {len(results)} entries')
    for i, item in enumerate(results[:15]):
        content = item.get('content', '')[:400]
        mem_id = item.get('memory_id', '?')
        layer = item.get('layer', '?')
        created = item.get('created_at', '?')
        print(f'\n--- Entry {i+1} ---')
        print(f'  memory_id: {mem_id}')
        print(f'  layer: {layer}')
        print(f'  created: {created}')
        print(f'  content: {content}')
else:
    print(f'Error: {r.status_code}')

# 2. Search TL-Ego's memories about self-cognition / identity
r2 = requests.post(f'{BASE}/api/v1/search', json={
    'query': 'who am I self-identity self-narrative introspection self-exploration',
    'user_ids': ['tl-ego'],
    'limit': 20
}, timeout=15)
print(f'\n\n=== Search: self-identity / self-exploration ===')
if r2.status_code == 200:
    data = r2.json()
    results = data.get('results', [])
    print(f'Found {len(results)} entries')
    for i, item in enumerate(results[:15]):
        content = item.get('content', '')[:400]
        mem_id = item.get('memory_id', '?')
        layer = item.get('layer', '?')
        created = item.get('created_at', '?')
        print(f'\n--- Entry {i+1} ---')
        print(f'  memory_id: {mem_id}')
        print(f'  layer: {layer}')
        print(f'  created: {created}')
        print(f'  content: {content}')
else:
    print(f'Error: {r2.status_code}')

# 3. Search TL-Ego graph-memory nodes from today
print(f'\n\n=== graph-memory: recent EVENT nodes (today) ===')
import sqlite3
from pathlib import Path
gm_db = Path.home() / '.openclaw' / 'graph-memory.db'
conn = sqlite3.connect(str(gm_db))
cur = conn.execute('''
    SELECT id, name, type, description, content, created_at
    FROM gm_nodes
    WHERE type = 'EVENT'
    ORDER BY id DESC
    LIMIT 30
''')
for row in cur.fetchall():
    print(f'  n-{row[0]}: name={row[1]} | type={row[2]}')
    desc = (row[3] or '')[:120]
    content = (row[4] or '')[:200]
    print(f'    desc: {desc}')
    print(f'    content: {content}')
    print(f'    created: {row[5]}')
    print()
conn.close()
