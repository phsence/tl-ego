"""
selection_gate.py
=================
Re-exports select_with_control from arena_simulator so that
tl_ego_server.py:22945  `from selection_gate import select_with_control`
works without modification.
"""
from arena_simulator import select_with_control  # noqa: F401
