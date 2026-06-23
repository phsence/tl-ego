# -*- coding: utf-8 -*-
"""Fix memory_bridge.py _write_graph to use gm_nodes table with correct columns."""
with open('memory_bridge.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Fix the INSERT in _write_graph
old = '''                """INSERT INTO nodes (type, content, summary, source_file, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (etype, content, summary, "tl-ego-server", now_ms)'''

new = '''                """INSERT INTO gm_nodes (type, name, description, content, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (etype, name, summary, content, now_ms)'''

if old in content:
    content = content.replace(old, new)
    with open('memory_bridge.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("✅ _write_graph INSERT fixed")
else:
    print("⚠️ old pattern not found, checking current state...")
    # Show the surrounding lines
    idx = content.find("INSERT INTO nodes")
    if idx >= 0:
        print(content[idx:idx+300])
    else:
        print("INSERT INTO nodes not found - maybe already fixed?")
        # Find INSERT INTO gm_nodes
        idx = content.find("INSERT INTO gm_nodes")
        if idx >= 0:
            print("Already has gm_nodes:")
            print(content[idx:idx+300])
