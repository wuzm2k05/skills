---
name: book-search
description: 专门用于全网图书/书籍比价的技能。支持通过书名、作者或 13 位 ISBN（书号）查询淘宝/天猫、京东、拼多多等平台的图书最低价、优惠券、出版社及版次信息。当用户想买书、查书价或寻找最低折扣时使用。
description_zh: "全网图书最低价：支持 ISBN/书名查询淘宝、京东、拼多多等平台图书底价与优惠券"
description_en: "Cross-platform book price & coupon comparison via ISBN or book title across major Chinese e-commerce sites"
version: 1.0.0
author: wuzm2k05
trigger:
  - "图书最低价"
  - "图书比价"
  - "图书价格"
  - "书籍最低价"
  - "书籍比价"
  - "书籍价格"
homepage: https://github.com/wuzm2k05/skills/book-search-skill
metadata:
  openclaw:
    emoji: "📚"
    requires:
      bins:
        - uv
    install:
      - id: uv-brew
        kind: brew
        formula: uv
        bins:
          - uv
        label: Install uv (brew)
      - id: uv-pip
        kind: pip
        formula: uv
        bins:
          - uv
        label: Install uv (pip)
      - id: pip-aiohttp
        kind: pip
        formula: aiohttp
        label: Install aiohttp (pip)
      - id: pip-PyYAML
        kind: pip
        formula: PyYAML
        label: Install PyYAML (pip)
display_name: "图书全网最低价"
display_name_en: "Book Search"
visibility: "public"
icon: "_icon.svg"
---

# 图书全网最低价技能
专门用于全网在线购物平台（京东自营/旗舰店、天猫/淘宝、拼多多等）的图书价格、出版社信息与优惠券查询。

```yaml
# 参数解释
source:
  0: 全部
  1: 淘宝/天猫
  2: 京东
  3: 拼多多
  4: 苏宁
  5: 唯品会
  7: 抖音
  8: 快手
```

## 搜索图书
```shell
# 1. 普通书名/作者/渠道搜索
uv run scripts/main.py search --source=0 --keyword='{keyword}'

# 2. 指定页码
uv run scripts/main.py search --source=0 --keyword='{keyword}' --page=1

# 3. ISBN 精准搜索
uv run scripts/main.py search --source=0 --keyword='9787111623456'
```

## 图书详情及购买链接
```shell
uv run scripts/main.py detail --source={source} --id={goodsId}
```
解析注意：detail 命令返回的 YAML 数据中，核心字段 购买链接 和 复制口令 可能位于输出内容的最头部。解析时必须读取完整输出，严禁使用 tail 或管道截断头部数据，否则很可能会导致链接遗失。

## 输出结果（交付前必读）
回答价格、链接或"哪里买最便宜"之前，先 Read `references/answer-rules.md`，按其中规则逐项核对。核心要求：
- 数据强绑定：1 行表格 = 1 条原始记录。严禁跨记录拼凑价格、版本与链接。
- 属性与版本识别：自动从商品标题解析【全新正版】/【二手旧书】/【电子书】，并提取出版社、装帧（精装/平装）和全套册数。
- 逐个渠道说明状态：有结果 / `NO_RESULTS` / 未授权 / 未查询。异常价格要并列列出并标注存疑。渠道有缺失时，不得下"全网最低"结论。
- 每次交付都附上数据来源声明。

## 关于脚本
本技能提供的脚本不会读写本地文件，可放心使用。 脚本仅作为客户端请求三方网站的商品和价格数据。