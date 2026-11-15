# 授权素材包最低成本采购求解器

交付多母版字体前, 从授权素材包中选出覆盖全部必需字符的最低总价组合。

## 规则

- 每个 UFO 目录是一个母版; 必需字符必须在**所有**母版中可用才算补齐。
- 包内带组件的字形, 其组件必须来自同包或已持有(现有 UFO / 其他已购包)的字形,
  且组件本身也要可用(递归闭包)。只堆轮廓却缺基础组件的包不算完成。
- 无解时, 逐字符列明在哪个母版、哪条部件链上缺货。

## 用法

```sh
python procurement/solver.py \
    --ufo path/to/Regular.ufo --ufo path/to/Bold.ufo \
    --packages procurement/packages.json
```

`--required` 可覆盖清单中的 `required` 字段。条目可以是字形名,
也可以是单个字符(经各母版 cmap 解析为字形名)。

## 清单格式 (packages.json)

```json
{
  "required": ["Á", "Adieresis", "colon"],
  "packages": [
    {
      "name": "pack-accents-full",
      "cost": 40,
      "masters": ["Regular", "Bold"],
      "glyphs": {
        "acute": [],
        "dieresis": ["dot"],
        "dot": []
      }
    }
  ]
}
```

- `masters` 可省略, 表示适用于全部母版。
- `glyphs` 的值是该字形的组件基字形列表; 空列表表示纯轮廓字形。

## 示例

- `packages.json`: 基于 `tests/data/` 两个 UFO 的可解场景
  (最低总价 185; 5 元的 `pack-punct-cheap-broken` 缺基础组件 `period`/`comma`, 不成立)。
- `packages.unsolvable.json`: 无解场景, 演示部件链缺货诊断
  (`Agrave -> grave` 在任何来源中都缺货)。
