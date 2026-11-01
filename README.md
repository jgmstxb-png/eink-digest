# eink-digest

每天 07:00（北京时间）自动从 RSS 源里筛出约 28 篇，输出一个 RSS 文件，给墨水屏 / 手机阅读器订阅。

## 订阅地址

```
https://jgmstxb-png.github.io/eink-digest/today.xml
```

任何支持 RSS 的阅读器都可以加（推荐 [有所闻 newsnook](https://github.com/t59688/newsnook)）。
GitHub Pages 直连国内一般可用；如果打不开，可在阅读器里设置代理，或用 `raw.githubusercontent.com` 镜像地址。

## 四道闸门

规则全部写在 `build_feed.py` 顶部，改数字即生效：

| 闸门 | 变量 | 当前值 |
|---|---|---|
| 1 时效 | `LAST_HOURS` | 36 小时 |
| 2 去重 | 链接 / 标题规范化 / 标题前 14 字 | 内置 |
| 3 字数 | `MIN_CHARS`、`MIN_CHARS_BY_CAT` | 400（汽车/科普 180） |
| 4 配额 | `feeds.json` 的 `quota` | ai9 / military5 / digital4 / finance4 / science3 / auto3 |

配额给 0，该分类就彻底消失。

## 目录

```
build_feed.py          主脚本（仅标准库）
feeds.json             源清单 + 分类配额
docs/today.xml         今日精选（GitHub Pages 对外发布）
docs/archive/YYYY-MM-DD.xml  历史归档
docs/index.html        落地页
.github/workflows/daily.yml  每天 07:00 自动跑
```

## 本地跑

```
python build_feed.py
```

## 为什么不自动写摘要

摘要给结论，原文给判断。摘要里只放来源、分类、字数，正文点进去读原文。
