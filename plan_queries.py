# -*- coding: utf-8 -*-
"""今晚的 MusicBrainz 查询规划器。

场景：今晚只剩一小批远端查询额度（quota，以“点数”计）。目录员给了
一批专题（topic），每个专题有优先级和一批必填字段。规则：

* 一次 get_*_by_id 查询可以带多个 include（由 musicbrainzngs 的
  VALID_INCLUDES 决定哪些 include 对该实体合法）。
* 某些组合不允许：user-tags / user-ratings / user-genres 需要认证
  （AUTH_REQUIRED_INCLUDES），今晚没有认证，一律不可用。
* 几个专题可能共同需要同一条 recording —— 一次查询能为多个专题
  同时供数。
* 已有缓存里的字段免费，不需要再查。
* 缺一个必填字段的专题不算交付，优先级一分不得。

目标：在额度内选出可“完整”交付、总优先级最高的专题集合，
然后按限速（默认 1 秒 1 请求，见 musicbrainz.set_rate_limit）
排出请求时间表。
"""

from itertools import combinations, product

from musicbrainzngs import musicbrainz as mb

# ---------------------------------------------------------------- 成本与额度

BASE_COST = 2          # 每次请求的固定成本（点数）
PER_INCLUDE_COST = 1   # 每多带一个 include 的追加成本
QUOTA = 14             # 今晚剩余额度（点数）

# 今晚没有认证：需要认证的 include 全部禁用（“不允许的组合”）
BANNED_INCLUDES = set(mb.AUTH_REQUIRED_INCLUDES)


def query_cost(includes):
    """一次查询的成本；空 include 集合表示不查询，成本为 0。"""
    if not includes:
        return 0
    return BASE_COST + PER_INCLUDE_COST * len(includes)


# ---------------------------------------------------------------- 实体与缓存

# 今晚涉及的对象（MBID 取自 query.py 里的示例）。
ENTITIES = {
    "rec1": ("recording", "cb4d4d70-930c-4d1a-a157-776de18be66a"),
    "art1": ("artist",    "952a4205-023d-4235-897c-6fdb6f58dfaa"),
    "rel1": ("release",   "e94757ff-2655-4690-b369-4012beba6114"),
    "rg1":  ("release-group", "9377d65d-ffd5-35d6-b64d-43f86ef9188d"),
    "wrk1": ("work",      "7e48685c-72dd-3a8b-9274-4777efb2aa75"),
    "lab1": ("label",     "aab2e720-bdd2-4565-afc2-460743585f16"),
}

# 已有缓存：实体 -> 已经抓过的字段（字段名即提供它的 include 名）。
CACHE = {
    "rec1": {"artist-credits"},
    "rel1": {"labels"},
}

# ---------------------------------------------------------------- 专题需求

# 每个专题：优先级 + 必填字段（实体键 -> 需要的字段集合）。
TOPICS = {
    "T1 录音指纹档案": {
        "priority": 9,
        "requires": {"rec1": {"isrcs", "artist-credits", "work-level-rels"}},
    },
    "T2 演出者关系": {
        "priority": 7,
        "requires": {"rec1": {"artist-rels"}, "art1": {"url-rels"}},
    },
    "T3 发行版本对比": {
        "priority": 8,
        "requires": {"rel1": {"labels", "recordings", "release-groups"},
                     "rg1": {"releases"}},
    },
    "T4 ISRC 交叉核对": {
        "priority": 6,
        "requires": {"rec1": {"isrcs"}, "rel1": {"isrcs"}},
    },
    "T5 作品词曲信息": {
        "priority": 5,
        "requires": {"wrk1": {"aliases", "artist-rels"},
                     "rec1": {"work-level-rels"}},
    },
    "T6 厂牌档案": {
        "priority": 4,
        "requires": {"lab1": {"aliases", "releases"}},
    },
    "T7 用户标签云": {  # 需要认证 include，今晚不可交付
        "priority": 10,
        "requires": {"rec1": {"user-tags"}},
    },
}


# ---------------------------------------------------------------- 能力模型

def allowed_includes(entity_key):
    """该实体今晚允许使用的 include：合法且不需要认证。"""
    entity_type = ENTITIES[entity_key][0]
    return set(mb.VALID_INCLUDES[entity_type]) - BANNED_INCLUDES


def needed_fields(entity_key):
    """所有专题对该实体要求的字段全集（未扣缓存）。"""
    fields = set()
    for topic in TOPICS.values():
        fields |= topic["requires"].get(entity_key, set())
    return fields


def include_options(entity_key):
    """该实体所有可选的单次查询方案：合法 include 子集 + 成本。

    一次查询带多个 include 总是比拆成多次便宜（固定成本只付一次），
    所以每个实体只需考虑“查一次，带子集 S”或“不查”。
    """
    fetchable = (needed_fields(entity_key) - CACHE.get(entity_key, set())) \
        & allowed_includes(entity_key)
    options = [(frozenset(), 0)]
    for size in range(1, len(fetchable) + 1):
        for subset in combinations(sorted(fetchable), size):
            subset = frozenset(subset)
            options.append((subset, query_cost(subset)))
    return options


# ---------------------------------------------------------------- 求解

def covered_fields(entity_key, chosen_includes):
    """选定方案后，该实体可用的字段 = 缓存 + 查到的 include。"""
    return CACHE.get(entity_key, set()) | set(chosen_includes)


def topic_deliverable(topic, plan):
    """专题的每个必填字段都被缓存或计划覆盖才算交付。"""
    for entity_key, fields in topic["requires"].items():
        if not fields <= covered_fields(entity_key, plan.get(entity_key, frozenset())):
            return False
    return True


def solve():
    entity_keys = sorted(ENTITIES)
    options_per_entity = [include_options(k) for k in entity_keys]

    best = None  # (总优先级, -成本, -请求数, 计划, 交付专题)
    for combo in product(*options_per_entity):
        plan = {k: inc for k, (inc, _cost) in zip(entity_keys, combo)}
        cost = sum(c for _inc, c in combo)
        if cost > QUOTA:
            continue
        delivered = [name for name, t in TOPICS.items()
                     if topic_deliverable(t, plan)]
        priority = sum(TOPICS[name]["priority"] for name in delivered)
        requests = sum(1 for inc, _c in combo if inc)
        key = (priority, -cost, -requests)
        if best is None or key > best[0]:
            best = (key, plan, delivered, cost, requests)
    return best


# ---------------------------------------------------------------- 限速排程

def schedule(plan, delivered):
    """按 musicbrainzngs 默认限速（1 请求 / 1 秒）排出请求时间表。

    服务专题多的查询排前面，让高价值数据尽早落库。
    """
    def serves(entity_key):
        return sum(1 for name in delivered
                   if entity_key in TOPICS[name]["requires"])

    queries = [(k, inc) for k, inc in plan.items() if inc]
    queries.sort(key=lambda q: -serves(q[0]))

    interval = mb.limit_interval / mb.limit_requests  # 1.0 秒
    rows = []
    for i, (entity_key, includes) in enumerate(queries):
        entity_type, mbid = ENTITIES[entity_key]
        rows.append({
            "start": i * interval,
            "call": "get_%s_by_id" % entity_type,
            "mbid": mbid,
            "includes": sorted(includes),
            "cost": query_cost(includes),
        })
    return rows


# ---------------------------------------------------------------- 报告

def main():
    (priority, neg_cost, _neg_req), plan, delivered, cost, requests = solve()

    print("额度: %d 点 | 单次请求成本: %d + %d/include" %
          (QUOTA, BASE_COST, PER_INCLUDE_COST))
    print("禁用 include（需认证）: %s\n" % ", ".join(sorted(BANNED_INCLUDES)))

    print("== 交付专题（总优先级 %d，花费 %d 点，%d 次请求）==" %
          (priority, len(delivered) and -neg_cost or 0, requests))
    for name in delivered:
        print("  [交付] %-14s 优先级 %d" % (name, TOPICS[name]["priority"]))
    for name in TOPICS:
        if name not in delivered:
            missing = []
            for entity_key, fields in TOPICS[name]["requires"].items():
                lack = fields - covered_fields(entity_key,
                                               plan.get(entity_key, frozenset()))
                if lack:
                    missing.append("%s:{%s}" % (entity_key, ",".join(sorted(lack))))
            banned = [f for fs in TOPICS[name]["requires"].values() for f in fs
                      if f in BANNED_INCLUDES]
            reason = "含需认证字段 %s" % banned if banned else "额度不足"
            print("  [放弃] %-14s 缺 %s（%s）" % (name, " ".join(missing), reason))

    print("\n== 请求计划（限速 %.1f 请求/秒）==" % (mb.limit_requests / mb.limit_interval))
    for row in schedule(plan, delivered):
        print("  t=%4.1fs  %-22s %s" % (row["start"], row["call"], row["mbid"]))
        print("           inc=%s  （%d 点）" % ("+".join(row["includes"]), row["cost"]))
    print("\n合计: %d 点 / 额度 %d 点" % (-neg_cost, QUOTA))


if __name__ == "__main__":
    main()
