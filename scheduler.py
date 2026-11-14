"""赛程日期推断 / Schedule date inference for old game records.

旧棋谱常见问题：
  * 有的棋谱只记录了年月（``month``），有的有精确日期（``date``），有的完全没有；
  * 轮号里混着补赛（make-up game）与分组，不能按轮号自动推断先后；
  * 同组同轮的对局允许跨日进行；
  * 同一棋手一天最多下一盘；
  * 补赛只遵守显式给出的先后关系。

本模块根据「可比赛日期 + 显式先后关系 + 少量已知日期」做约束传播与搜索，
回答两个问题：

  1. 每盘棋仍可能在哪几天举行（:meth:`Schedule.analyze` 返回的
     :attr:`Analysis.possible`）；只剩一天的对局即被唯一定位
     （:meth:`Analysis.unique_dates`）。
  2. 若某组无解，找出互相挤占、导致不可行的最小对局集合
     （:attr:`Analysis.conflicts`，即 minimal unsat cores）。
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

Date = datetime.date
YearMonth = Tuple[int, int]


@dataclass(frozen=True)
class Game:
    """一盘棋。``date`` 为精确日期，``month`` 为只知年月的记录。"""

    gid: str
    white: str
    black: str
    group: str
    round: Any = None
    date: Optional[Date] = None
    month: Optional[YearMonth] = None
    makeup: bool = False

    @property
    def players(self) -> Tuple[str, str]:
        return (self.white, self.black)


@dataclass
class Analysis:
    """分析结果。"""

    feasible: bool
    possible: Dict[str, FrozenSet[Date]] = field(default_factory=dict)
    conflicts: List[FrozenSet[str]] = field(default_factory=list)

    def unique_dates(self) -> Dict[str, Date]:
        """返回被唯一定位（只剩一个可能日期）的对局。"""
        return {gid: next(iter(days)) for gid, days in self.possible.items() if len(days) == 1}


class Schedule:
    """赛程约束。

    - ``set_playable_dates`` 设定某组（或默认）可比赛的日期集合；
    - ``add_precedence(a, b)`` 表示 a 必须严格早于 b（补赛只靠这种显式关系排序）；
    - ``add_round_order`` 是便捷方法：把某组内两轮之间的所有对局两两加上先后。
    """

    def __init__(self) -> None:
        self.games: Dict[str, Game] = {}
        self._playable: Dict[Optional[str], Set[Date]] = {}
        self._round_playable: Dict[Tuple[str, Any], Set[Date]] = {}
        self._precedence: List[Tuple[str, str]] = []

    # ------------------------------------------------------------------ input

    def add_game(self, game: Game) -> None:
        if game.gid in self.games:
            raise ValueError(f"duplicate game id: {game.gid}")
        self.games[game.gid] = game

    def set_playable_dates(self, group: Optional[str], dates: Iterable[Date]) -> None:
        """设定某组的可比赛日期；``group=None`` 表示所有组的默认值。"""
        self._playable[group] = set(dates)

    def set_round_playable_dates(self, group: str, rnd: Any, dates: Iterable[Date]) -> None:
        """进一步限定某组某轮的可比赛日期（与组级日期取交集）。"""
        self._round_playable[(group, rnd)] = set(dates)

    def add_precedence(self, before_gid: str, after_gid: str) -> None:
        """显式先后：``before_gid`` 必须严格早于 ``after_gid`` 举行。"""
        self._precedence.append((before_gid, after_gid))

    def add_round_order(self, group: str, earlier_round: Any, later_round: Any) -> None:
        """确认某组内 ``earlier_round`` 的所有对局早于 ``later_round`` 的所有对局。

        补赛（``makeup=True``）不参与轮次排序，只遵守显式给出的先后。
        """
        earlier = [g.gid for g in self.games.values()
                   if g.group == group and g.round == earlier_round and not g.makeup]
        later = [g.gid for g in self.games.values()
                 if g.group == group and g.round == later_round and not g.makeup]
        for a in earlier:
            for b in later:
                self.add_precedence(a, b)

    # ------------------------------------------------------------- derivation

    def _base_domain(self, game: Game) -> Set[Date]:
        playable = self._playable.get(game.group, self._playable.get(None))
        if playable is None:
            raise ValueError(f"no playable dates defined for group {game.group!r}")
        domain = set(playable)
        round_dates = self._round_playable.get((game.group, game.round))
        if round_dates is not None:
            domain &= round_dates
        if game.date is not None:
            domain &= {game.date}
        if game.month is not None:
            year, month = game.month
            domain = {d for d in domain if (d.year, d.month) == (year, month)}
        return domain

    def _players_index(self, gids: Iterable[str]) -> Dict[str, List[str]]:
        index: Dict[str, List[str]] = {}
        for gid in gids:
            game = self.games[gid]
            for player in game.players:
                index.setdefault(player, []).append(gid)
        return index

    # ---------------------------------------------------------------- solving

    @staticmethod
    def _propagate(domains: Dict[str, Set[Date]],
                   precedence: Sequence[Tuple[str, str]],
                   players: Dict[str, List[str]]) -> bool:
        """弧一致性传播；返回 False 表示出现空域（不可行）。"""
        changed = True
        while changed:
            changed = False
            for a, b in precedence:
                da, db = domains[a], domains[b]
                if not da or not db:
                    return False
                max_b, min_a = max(db), min(da)
                new_a = {d for d in da if d < max_b}
                new_b = {d for d in db if d > min_a}
                if not new_a or not new_b:
                    return False
                if new_a != da:
                    domains[a] = new_a
                    changed = True
                if new_b != db:
                    domains[b] = new_b
                    changed = True
            for gids in players.values():
                fixed: Set[Date] = set()
                for gid in gids:
                    if len(domains[gid]) == 1:
                        fixed |= domains[gid]
                for gid in gids:
                    domain = domains[gid]
                    if len(domain) > 1:
                        reduced = domain - fixed
                        if not reduced:
                            return False
                        if reduced != domain:
                            domains[gid] = reduced
                            changed = True
                union: Set[Date] = set()
                for gid in gids:
                    union |= domains[gid]
                if len(union) < len(gids):  # Hall 条件：人手不够分
                    return False
        return True

    def _solve(self, domains: Dict[str, Set[Date]],
               precedence: Sequence[Tuple[str, str]],
               players: Dict[str, List[str]]) -> Optional[Dict[str, Date]]:
        """回溯搜索一个可行安排；无可行解返回 None。"""
        domains = {gid: set(domain) for gid, domain in domains.items()}
        if not self._propagate(domains, precedence, players):
            return None
        todo = [gid for gid, domain in domains.items() if len(domain) > 1]
        if not todo:
            return {gid: next(iter(domain)) for gid, domain in domains.items()}
        gid = min(todo, key=lambda g: len(domains[g]))
        for day in sorted(domains[gid]):
            sub = {g: set(d) for g, d in domains.items()}
            sub[gid] = {day}
            solution = self._solve(sub, precedence, players)
            if solution is not None:
                return solution
        return None

    def _subproblem(self, gids: Sequence[str]) -> Tuple[Dict[str, Set[Date]],
                                                        List[Tuple[str, str]],
                                                        Dict[str, List[str]]]:
        gid_set = set(gids)
        domains = {gid: self._base_domain(self.games[gid]) for gid in gids}
        precedence = [(a, b) for a, b in self._precedence if a in gid_set and b in gid_set]
        players = self._players_index(gids)
        return domains, precedence, players

    def _satisfiable(self, gids: Sequence[str]) -> bool:
        domains, precedence, players = self._subproblem(gids)
        return self._solve(domains, precedence, players) is not None

    def _minimal_cores(self, gids: Sequence[str]) -> List[FrozenSet[str]]:
        """找出子集内互相挤占的最小对局集合（minimal unsat cores）。"""
        gids = sorted(gids)
        if len(gids) > 14:  # 枚举代价过高，退化为删除法求一个核
            core = list(gids)
            for gid in list(core):
                trial = [g for g in core if g != gid]
                if trial and not self._satisfiable(trial):
                    core = trial
            return [frozenset(core)] if core else []
        cores: List[FrozenSet[str]] = []
        for size in range(1, len(gids) + 1):
            for subset in combinations(gids, size):
                if any(set(core) <= set(subset) for core in cores):
                    continue
                if not self._satisfiable(list(subset)):
                    cores.append(frozenset(subset))
        return cores

    # ---------------------------------------------------------------- analyze

    def analyze(self) -> Analysis:
        """求解每盘的可能日期；无解时给出互相挤占的最小对局集合。"""
        all_gids = sorted(self.games)
        domains, precedence, players = self._subproblem(all_gids)
        solution = self._solve(domains, precedence, players)

        if solution is None:
            conflicts: List[FrozenSet[str]] = []
            groups = sorted({self.games[gid].group for gid in all_gids})
            covered: Set[str] = set()
            for group in groups:
                group_gids = [gid for gid in all_gids if self.games[gid].group == group]
                if not self._satisfiable(group_gids):
                    cores = self._minimal_cores(group_gids)
                    conflicts.extend(cores)
                    for core in cores:
                        covered |= set(core)
            if not conflicts:  # 各组单独可行，是跨组棋手互相挤占
                conflicts = self._minimal_cores(all_gids)
            return Analysis(feasible=False, conflicts=conflicts)

        witnessed: Dict[str, Set[Date]] = {gid: {day} for gid, day in solution.items()}
        for gid in all_gids:
            for day in sorted(domains[gid]):
                if day in witnessed[gid]:
                    continue
                sub = {g: set(d) for g, d in domains.items()}
                sub[gid] = {day}
                found = self._solve(sub, precedence, players)
                if found is not None:
                    for g, d in found.items():
                        witnessed[g].add(d)
        possible = {gid: frozenset(days) for gid, days in witnessed.items()}
        return Analysis(feasible=True, possible=possible)
