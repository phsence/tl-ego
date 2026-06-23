import sys, os
sys.stdout.reconfigure(encoding='utf-8')

path = 'memory_bridge.py'
with open(path, 'r', encoding='utf-8') as f:
    content = f.read()

changes = 0

# Fix 1: WHERE clause summary -> description
old1 = '(content LIKE ? OR summary LIKE ? OR type LIKE ?)'
new1 = '(content LIKE ? OR description LIKE ? OR type LIKE ?)'
if old1 in content:
    content = content.replace(old1, new1)
    changes += 1
    print('[OK] Fix 1: WHERE clause summary -> description')
else:
    print('[SKIP] Fix 1: pattern not found')

# Fix 2: INSERT INTO gm_nodes - already done in earlier fix, just verify
if 'INSERT INTO gm_nodes (type, name, description, content, created_at)' in content:
    print('[OK] Fix 2: INSERT already uses gm_nodes')
else:
    print('[WARN] Fix 2: INSERT still uses old table')
    old2 = 'INSERT INTO nodes'
    if old2 in content:
        print(f'  Found: {content[content.index(old2):content.index(old2)+120]}')

# Fix 3: also update _write_graph docstring
old3 = 'schema: id, type, content, summary, created_at, source_file'
new3 = 'schema: id, type, name, description, content, created_at'
if old3 in content:
    content = content.replace(old3, new3)
    changes += 1
    print('[OK] Fix 3: docstring updated')

with open(path, 'w', encoding='utf-8') as f:
    f.write(content)

print(f'\nTotal changes: {changes}')
print('File updated successfully.')
