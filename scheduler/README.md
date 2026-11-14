# 知识条目分批排期器

根据每篇条目 front matter 里声明的依赖，计算指定专题**最早可交付的分批入库清单**。

## Front matter 约定

```yaml
---
id: topic                      # 条目 id（省略时取文件名）
prerequisites: [base-a]        # 先决条目：全部必须可用（别名 先决条目）
alternative_groups:            # 可选替代条目：每组任意一个可用即可
  - [slow-path, quick-path]    # 别名 alternatives / 可选替代条目 / 替代条目
estimate_hours: 3              # 预计导入耗时（别名 duration / 预计导入耗时）
---
```

替代组也支持扁平列表（整体视为一组）和映射写法 `先决条目: [替代品...]`。

## 排期语义

- 每晚只有一个入库时段，对应输出中的一个批次。
- 当晚入库的条目**下一晚**才能被引用，因此每条依赖边至少相隔一晚。
- 同一晚可以入库多篇互不阻塞的条目；如需限制单晚总工时，传 `--capacity`。
- 替代组按 OR 处理：替代来源之间即使存在循环，只要另有一条路径已经可用，
  循环不会堵死排期（求解器对"可发布条目"做不动点迭代）。
- 专题不可发布时，输出沿依赖链追出的根因：目录中缺失、或在所有替代路径上
  都无法获得的具体条目；纯先决闭环且无外部出口时也会明确指出。

## 用法

```shell
python -m scheduler <条目目录> <专题id>                 # 不限制单晚工时
python -m scheduler <条目目录> <专题id> --capacity 8    # 每晚最多 8 小时
```

输出按晚列出批次、每批条目与工时，并给出替代组最终选择了哪条路径。
程序化调用见 `scheduler.build_plan(catalog, target)`，测试见
`tests/test_scheduler.py`。
