"""2026-11-03: 经验生长不能静默丢。

线上实测: `grow_dropped_total=68,990` 而 `edges_grown_total=870` —— 绝大多数共激活对
根本没进图。两个原因:
  1. `cnt == COACTIVATION_THRESHOLD` 是一次性判定: 队列满时被丢掉的配对 cnt 已经
     越过阈值, 之后永远不再入队 = 永久丢经验
  2. 阈值被探针改成 1 (共激活一次就算), 每 tick 44,850 对全部过阈值, 队列必爆

本检查断言核心不变量: **没挤进队列的配对, 下个 tick 还能再进**(只延后, 不丢弃)。

跑法: python test_grow_queue.py
"""
import edge_sparsifier as ES

ES.COACTIVATION_THRESHOLD = 1     # 让第一次共激活就过阈值
ES.MAX_NEW_EDGES_PER_TICK = 0     # 只看入队, 不消费
ES._NEURON_TYPE_REF = {}

# 容量只有 1 → 三对里进 1 个, 另外 2 个延后, 一条都不算丢弃
ES._GROW_QUEUE_MAX = 1
ES.coactivation_update([{1, 2, 3}])
assert len(ES._pending_grow) == 1, list(ES._pending_grow)
assert ES._grow_deferred_total == 2, ES._grow_deferred_total
assert ES._grow_dropped_total == 0, "延后的配对被记成丢弃了"

# 排空队列 (模拟 drain), 放开容量再来一个 tick: 此时三对的 cnt 已经是 2。
# 旧实现用 == 判定, 延后的 2 对永远不会再入队 → 这里会只有 1 个。
while ES._pending_grow:
    ES._enqueued_keys.discard(ES._pending_grow.popleft())
ES._GROW_QUEUE_MAX = 10
ES.coactivation_update([{1, 2, 3}])
assert len(ES._pending_grow) == 3, (
    f"延后的配对没有重新入队 ({len(ES._pending_grow)}/3) —— 经验被永久丢弃")

print("OK: 队列满只延后不丢弃, 下个 tick 重新入队; 阈值可调 =",
      ES.COACTIVATION_THRESHOLD)
