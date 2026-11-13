#!/usr/bin/env python3
"""Guard the STDP prefetch batch bound against regression.

Run: python -B tests/test_stdp_prefetch_budget.py
"""
import ast
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent / "tl_ego_server.py"
source = SERVER.read_text(encoding="utf-8", errors="replace")
tree = ast.parse(source)
apply_stdp = next(
    node for node in tree.body
    if isinstance(node, ast.FunctionDef) and node.name == "apply_stdp"
)
assignments = {
    node.targets[0].id: node.value.value
    for node in ast.walk(apply_stdp)
    if isinstance(node, ast.Assign)
    and len(node.targets) == 1
    and isinstance(node.targets[0], ast.Name)
    and node.targets[0].id == "_prefetch_batch_size"
    and isinstance(node.value, ast.Constant)
}
assert assignments == {"_prefetch_batch_size": 25}, assignments
loop = next(
    node for node in ast.walk(apply_stdp)
    if isinstance(node, ast.For)
    and isinstance(node.iter, ast.Call)
    and isinstance(node.iter.func, ast.Name)
    and node.iter.func.id == "range"
    and any(
        isinstance(arg, ast.Name) and arg.id == "_prefetch_batch_size"
        for arg in node.iter.args
    )
)
assert any(
    isinstance(node, ast.Call)
    and isinstance(node.func, ast.Name)
    and node.func.id == "_ensure_edges_batch"
    for node in ast.walk(loop)
)
assert any(isinstance(node, ast.If) for node in loop.body), "deadline guard removed"
print("test_stdp_prefetch_budget: batch=25, deadline guard present")
