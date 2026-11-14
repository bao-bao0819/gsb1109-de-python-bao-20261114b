"""镜像站格式偏好对账：最少分期分析。

输入：按时间排好的 h11 请求/响应观测。每条观测给出
  - available: 库存记录证明当时可用的格式
  - accepted:  请求 Accept 头中等权（同 q 值）接受的格式
  - chosen:    响应实际选中的格式
只在"当时可用且等权接受"的格式集合内推断显示偏好：
chosen 胜过池中其余每一个格式。

一个时期能被某种固定严格次序解释，当且仅当该期显示偏好有向图无环
（严格全序存在 ⟺ 无环，拓扑序即见证次序）。
"""

from __future__ import annotations

OBSERVATIONS = [
    # t1：库存只有 A、B，等权接受 A、B，选中 A  =>  A ≻ B
    {"time": "t1", "available": {"A", "B"}, "accepted": {"A", "B"}, "chosen": "A"},
    # t2：库存只有 B、C，等权接受 B、C，选中 B  =>  B ≻ C
    {"time": "t2", "available": {"B", "C"}, "accepted": {"B", "C"}, "chosen": "B"},
    # t3：库存只有 A、C，等权接受 A、C，选中 C  =>  C ≻ A
    {"time": "t3", "available": {"A", "C"}, "accepted": {"A", "C"}, "chosen": "C"},
]

INF = float("inf")


def revealed_edges(obs):
    """从观测序列推断显示偏好边集：chosen ≻ 其他可用且等权接受的格式。"""
    edges = set()
    for o in obs:
        pool = (o["available"] & o["accepted"]) - {o["chosen"]}
        for loser in pool:
            edges.add((o["chosen"], loser))
    return edges


def topo_order(edges):
    """无环则返回一个拓扑序（见证严格次序），有环则返回 None。"""
    nodes = {x for e in edges for x in e}
    indeg = {n: 0 for n in nodes}
    adj = {n: [] for n in nodes}
    for a, b in edges:
        adj[a].append(b)
        indeg[b] += 1
    queue = sorted(n for n in nodes if indeg[n] == 0)
    order = []
    while queue:
        n = queue.pop(0)
        order.append(n)
        for m in adj[n]:
            indeg[m] -= 1
            if indeg[m] == 0:
                queue.append(m)
    return order if len(order) == len(nodes) else None


def find_cycle(edges):
    """在无环检测失败时返回一个有向环，用于解释矛盾。"""
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {}
    stack = []

    def dfs(u):
        color[u] = GRAY
        stack.append(u)
        for v in adj.get(u, []):
            if color.get(v, WHITE) == GRAY:
                return stack[stack.index(v):] + [v]
            if color.get(v, WHITE) == WHITE:
                r = dfs(v)
                if r:
                    return r
        stack.pop()
        color[u] = BLACK
        return None

    for n in list(adj):
        if color.get(n, WHITE) == WHITE:
            r = dfs(n)
            if r:
                return r
    return None


def main():
    obs = OBSERVATIONS
    n = len(obs)

    # valid[i][j]: obs[i:j] 能否由一种固定严格次序解释
    valid = [[False] * (n + 1) for _ in range(n + 1)]
    for i in range(n):
        edges = set()
        for j in range(i + 1, n + 1):
            edges |= revealed_edges([obs[j - 1]])
            valid[i][j] = topo_order(edges) is not None

    # f[i] = 后缀 obs[i:] 的最少期数；g[i] = 前缀 obs[:i] 的最少期数
    f = [INF] * (n + 1)
    f[n] = 0
    for i in range(n - 1, -1, -1):
        for j in range(i + 1, n + 1):
            if valid[i][j]:
                f[i] = min(f[i], 1 + f[j])
    g = [INF] * (n + 1)
    g[0] = 0
    for j in range(1, n + 1):
        for i in range(j):
            if valid[i][j]:
                g[j] = min(g[j], g[i] + 1)

    opt = f[0]
    print(f"观测数: {n}，最少需要分成 {opt} 个连续时期\n")

    # 逐条观测的显示偏好
    for o in obs:
        edges = revealed_edges([o])
        pretty = ", ".join(f"{a} ≻ {b}" for a, b in sorted(edges))
        print(f"  {o['time']}: 可用 {sorted(o['available'])}，"
              f"等权接受 {sorted(o['accepted'])}，选中 {o['chosen']}  =>  {pretty}")
    print()

    # 全序列为何不可并存
    all_edges = revealed_edges(obs)
    cycle = find_cycle(all_edges)
    if cycle:
        print("整批不可由单一严格次序解释，存在偏好环：")
        print("  " + " ≻ ".join(cycle))
        print("  严格次序必须传递且非对称：前两条推出 "
              f"{cycle[0]} ≻ {cycle[2]}，与第三条直接冲突。\n")

    # 给出一个最优分期及每期见证次序
    print("一个最优分期：")
    i = 0
    k = 0
    while i < n:
        for j in range(n, i, -1):
            if valid[i][j] and f[i] == 1 + f[j]:
                k += 1
                order = topo_order(revealed_edges(obs[i:j]))
                seg = ", ".join(o["time"] for o in obs[i:j])
                print(f"  第 {k} 期 [{seg}]: 见证次序 {' ≻ '.join(order)}")
                i = j
                break
    print()

    # 分界分类：b 表示 obs[b-1] 与 obs[b] 之间的分界
    print("分界分析（位置用观测序号表示）：")
    for b in range(1, n):
        achievable = g[b] + f[b] == opt
        if not achievable:
            print(f"  {obs[b-1]['time']} | {obs[b]['time']}: 不可能出现在任何最少分期中")
            continue
        # 是否存在不含此分界的最优分期：某段 [s,e) 跨过 b 且 g[s]+1+f[e]==opt
        avoidable = any(
            valid[s][e] and g[s] + 1 + f[e] == opt
            for s in range(b)
            for e in range(b + 1, n + 1)
        )
        kind = "可移动（有的最少分期在此切，有的不在）" if avoidable else "强制（所有最少分期都在此切）"
        print(f"  {obs[b-1]['time']} | {obs[b]['time']}: {kind}")


if __name__ == "__main__":
    main()
