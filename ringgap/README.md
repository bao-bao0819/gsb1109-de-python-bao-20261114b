# ringgap — 环形序号缺件归类

设备按递增绝对序号产出采集文件，站点只保留最近 256 个序号，文件名取
序号的两位十六进制（`00`..`ff`），序号越过 255 后文件名循环、旧文件被
覆盖。本模块依据两次目录快照、轮次锚点、收件回执和设备声明的最大产出
速率，把缺件分成三类：

- `overwritten`：旧代次缺件已滚出保留窗口，同名位置被新代次占据，无法再请求；
- `requestable`：缺件仍是当前窗口内的那个序号，还可以请求补传；
- `uncertain`：现有证据相容多种代次（设备可能绕了多圈），不能唯一归类。

另外给出两个辅助桶：`confirmed`（候选序号都已有收件回执）和
`fulfilled`（早先的缺件后来被补传）。

## 为什么不能只按短路径补号

文件名只有两位十六进制，同一个名字在不同圈对应不同绝对序号。两次快照
之间设备可能绕多圈，因此"快照 A 缺 `0a`、快照 B 也缺 `0a`"并不自动
意味着同一个缺件。本模块枚举所有与下列证据相容的
`(快照 B 头序号, 快照 A 头序号)` 组合：

- 速率上限：快照间产出数 `d ∈ [0, floor(rate × Δt)]`（下界为 0，
  因为新出现的名字可能只是旧缺口补传，不推进头序号）；
- 锚点：某时刻某名字对应的绝对序号，过去锚点给上界、未来锚点给下界；
- 保留窗口：现存文件的代表序号必须落在 `[max(0, head-255), head]`；
- 消失一致性：A 中存在、B 中消失的文件只能因老化换代，同代次不能凭空消失。

某个槽位只有在所有相容状态下结论完全一致时才自信归类，否则进入
`uncertain`，并在 `possible_sequences` 中列出全部候选绝对序号。

## 请求前复核

`build_request_plan` 只收录 `requestable` 桶；计划落实前调用
`reconcile`，用当前目录重新枚举相容状态：

- 目标序号在所有状态下都已滚出窗口 → `overwritten`，绝不把同名新件
  登记成旧缺件；
- 窗口边界在部分状态下已越过目标序号 → `uncertain`；
- 同名文件已出现且代次一致 → `fulfilled`；
- 名字仍缺且序号仍在窗口 → `keep`，可以发请求。

## JSON 入口

```bash
python -m ringgap example_input.json
```

输入字段：

```json
{
  "rate_per_sec": 1.0,
  "snapshot_a": {"time": 0, "files": {"00": 0, "01": 0}},
  "snapshot_b": {"time": 10, "files": {"09": 10, "0b": 10}},
  "anchors": [{"name": "ff", "sequence": 255, "time": 0}],
  "receipts": [10],
  "current": {"time": 12, "files": {"09": 12}}
}
```

- `files` 的键是两位十六进制文件名，值是文件时间戳（仅留存，不参与判定）；
- `anchors[].sequence` 必须与 `name` 的槽位同余（`sequence % 256 == slot`）；
- `receipts` 是已经登记在案的绝对序号；
- `current` 可选，给出时额外输出 `request_plan` 与 `reconcile` 复核结果。

## 编程接口

```python
from ringgap import Anchor, Snapshot, classify, build_request_plan, reconcile

snapshot_a = Snapshot.from_mapping(0, {"00": 0, "02": 0})
snapshot_b = Snapshot.from_mapping(10, {"09": 10, "0b": 10})
anchors = [Anchor.build("ff", 255, 0)]

result = classify(snapshot_a, snapshot_b, rate=1.0, anchors=anchors)
plan = build_request_plan(result, created_at=12)
review = reconcile(plan, snapshot_b, snapshot_current, 1.0, anchors)
```
