import datetime
import unittest

from scheduler import Analysis, Game, Schedule

D = lambda day: datetime.date(2026, 11, day)


def make_schedule(days, games, group="A"):
    schedule = Schedule()
    schedule.set_playable_dates(group, [D(d) for d in days])
    for game in games:
        schedule.add_game(game)
    return schedule


class PossibleDatesTestCase(unittest.TestCase):
    def test_free_round_robin(self):
        # 三人循环赛、三个比赛日、无任何已知日期：每盘三天都可能。
        games = [
            Game("g1", "A", "B", "G", round=1),
            Game("g2", "B", "C", "G", round=2),
            Game("g3", "A", "C", "G", round=3),
        ]
        analysis = make_schedule([1, 2, 3], games, "G").analyze()
        self.assertTrue(analysis.feasible)
        for gid in ("g1", "g2", "g3"):
            self.assertEqual(analysis.possible[gid], frozenset({D(1), D(2), D(3)}))

    def test_exact_date_pins_game(self):
        games = [
            Game("g1", "A", "B", "G", date=D(2)),
            Game("g2", "B", "C", "G"),
        ]
        analysis = make_schedule([1, 2, 3], games, "G").analyze()
        self.assertEqual(analysis.possible["g1"], frozenset({D(2)}))
        self.assertEqual(analysis.possible["g2"], frozenset({D(1), D(3)}))

    def test_month_only_record(self):
        # 旧棋谱只知 2026 年 11 月：被限制在 11 月内的可比赛日。
        nov = [datetime.date(2026, 11, d) for d in (5, 6, 7)]
        oct_ = [datetime.date(2026, 10, 30)]
        schedule = Schedule()
        schedule.set_playable_dates("G", set(nov) | set(oct_))
        schedule.add_game(Game("g1", "A", "B", "G", month=(2026, 11)))
        analysis = schedule.analyze()
        self.assertEqual(analysis.possible["g1"], frozenset(nov))

    def test_unique_localization_via_opponents(self):
        # g3(A-B) 无日期记录；A 在 1 日有比赛、B 在 2 日有比赛，
        # 靠两个对手的其他场次把 g3 唯一挤到 3 日。
        games = [
            Game("g1", "A", "C", "G", date=D(1)),
            Game("g2", "B", "D", "G", date=D(2)),
            Game("g3", "A", "B", "G"),
        ]
        analysis = make_schedule([1, 2, 3], games, "G").analyze()
        self.assertEqual(analysis.unique_dates(), {"g1": D(1), "g2": D(2), "g3": D(3)})

    def test_same_round_may_span_days(self):
        # 同组同轮可以跨日：同轮两盘共用一个棋手，两个比赛日刚好错开。
        games = [
            Game("g1", "A", "B", "G", round=1),
            Game("g2", "A", "C", "G", round=1),
        ]
        analysis = make_schedule([1, 2], games, "G").analyze()
        self.assertTrue(analysis.feasible)
        self.assertEqual(analysis.possible["g1"], frozenset({D(1), D(2)}))
        self.assertEqual(analysis.possible["g2"], frozenset({D(1), D(2)}))

    def test_makeup_only_explicit_precedence(self):
        # 补赛不按轮号自动排序：无显式先后时 g2 虽轮号大却可在首日。
        games = [
            Game("g1", "A", "B", "G", round=1, date=D(2)),
            Game("g2", "C", "D", "G", round=5, makeup=True),
        ]
        analysis = make_schedule([1, 2, 3], games, "G").analyze()
        self.assertEqual(analysis.possible["g2"], frozenset({D(1), D(2), D(3)}))

        # 加上显式先后后，补赛必须严格晚于 g1。
        schedule = make_schedule([1, 2, 3], games, "G")
        schedule.add_precedence("g1", "g2")
        analysis = schedule.analyze()
        self.assertEqual(analysis.possible["g2"], frozenset({D(3)}))

    def test_round_order_constraint(self):
        games = [
            Game("g1", "A", "B", "G", round=1),
            Game("g2", "C", "D", "G", round=2),
        ]
        schedule = make_schedule([1, 2], games, "G")
        schedule.add_round_order("G", 1, 2)
        analysis = schedule.analyze()
        self.assertEqual(analysis.unique_dates(), {"g1": D(1), "g2": D(2)})

    def test_round_order_skips_makeup(self):
        # 轮次先后不约束补赛：补赛挂在第 1 轮，却可晚于第 2 轮。
        games = [
            Game("g1", "A", "B", "G", round=1, date=D(1)),
            Game("g2", "C", "D", "G", round=2, date=D(2)),
            Game("g3", "E", "F", "G", round=1, makeup=True),
        ]
        schedule = make_schedule([1, 2, 3], games, "G")
        schedule.add_round_order("G", 1, 2)
        analysis = schedule.analyze()
        self.assertEqual(analysis.possible["g3"], frozenset({D(1), D(2), D(3)}))

    def test_round_playable_dates(self):
        games = [Game("g1", "A", "B", "G", round=2)]
        schedule = make_schedule([1, 2, 3], games, "G")
        schedule.set_round_playable_dates("G", 2, [D(3)])
        analysis = schedule.analyze()
        self.assertEqual(analysis.possible["g1"], frozenset({D(3)}))


class ConflictTestCase(unittest.TestCase):
    def test_player_overbooked(self):
        # 棋手 A 有 3 盘棋但只有 2 个比赛日：三盘互相挤占。
        games = [
            Game("g1", "A", "B", "G"),
            Game("g2", "A", "C", "G"),
            Game("g3", "A", "D", "G"),
            Game("g4", "B", "C", "G"),
        ]
        analysis = make_schedule([1, 2], games, "G").analyze()
        self.assertFalse(analysis.feasible)
        self.assertEqual(set(analysis.conflicts),
                         {frozenset({"g1", "g2", "g3"}), frozenset({"g1", "g2", "g4"})})

    def test_impossible_exact_date(self):
        # 精确日期不在可比赛日内：该盘自身就是冲突核。
        games = [Game("g1", "A", "B", "G", date=D(9))]
        analysis = make_schedule([1, 2], games, "G").analyze()
        self.assertFalse(analysis.feasible)
        self.assertEqual(analysis.conflicts, [frozenset({"g1"})])

    def test_contradictory_precedence(self):
        games = [
            Game("g1", "A", "B", "G", date=D(2)),
            Game("g2", "C", "D", "G", date=D(1)),
        ]
        schedule = make_schedule([1, 2], games, "G")
        schedule.add_precedence("g1", "g2")  # g1 必须早于 g2，与已知日期矛盾
        analysis = schedule.analyze()
        self.assertFalse(analysis.feasible)
        self.assertEqual(analysis.conflicts, [frozenset({"g1", "g2"})])

    def test_cross_group_player_clash(self):
        # 两组各自可行，但共享棋手在唯一比赛日撞车。
        schedule = Schedule()
        schedule.set_playable_dates("G1", [D(1)])
        schedule.set_playable_dates("G2", [D(1)])
        schedule.add_game(Game("g1", "A", "B", "G1"))
        schedule.add_game(Game("g2", "A", "C", "G2"))
        analysis = schedule.analyze()
        self.assertFalse(analysis.feasible)
        self.assertEqual(analysis.conflicts, [frozenset({"g1", "g2"})])


if __name__ == "__main__":
    unittest.main()
