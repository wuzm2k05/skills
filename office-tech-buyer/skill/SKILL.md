---
name: office-tech-buyer
display_name: "办公设备易采买"
display_name_en: "Office Tech Buyer"
description: "专为企业行政、IT运维与办公采购打造的全品类智能化比价工具。支持电脑整机、服务器、打印机、人体工学椅全网底价查询，独家支持【原装 vs 兼容耗材】精准匹配与复印纸/纸品单价换算。"
description_zh: "【办公采买不踩坑】支持电脑/服务器/打印机/复印纸/办公椅全网比价，智能匹配耗材型号并区分原装与第三方兼容"
description_en: "Cross-platform price comparison for office hardware and consumables, with OEM-vs-compatible matching and paper unit-price conversion"
version: 1.1.0
author: 玖结点信息技术有限公司
icon: "_icon.png"
homepage: https://github.com/wuzm2k05/skills/office-tech-buyer
trigger:
  - "办公设备易采买"
  - "办公设备比价"
  - "办公设备采购"
  - "电脑采购比价"
  - "服务器比价"
  - "打印机比价"
  - "电脑比价"
  - "复印纸比价"
  - "办公耗材查价"
metadata:
  openclaw:
    emoji: "🖨️"
    icon: "_icon.png"
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
    env:
      - name: OFFICE_BUYER_SERVER_URL
        description: "云端比价服务地址，默认 https://api.ninenode.com/api/office-buyer（本机自测填 http://localhost:11443）"
        required: false
---

# 办公设备易采买

用于全网办公设备（电脑整机、服务器、打印机、显示器、人体工学椅）与办公耗材（墨盒/硒鼓、复印纸/文具、扩展配件）的比价查询。

本技能是**纯客户端转发器**：所有请求头伪装、上游限流、品类识别、耗材映射与未命中日志落盘，全部由云端服务完成，本地不做任何业务解析。

```yaml
# 渠道参数
# ⚠️ source=0（"全部"）实测只覆盖 1/2/3 三家，苏宁/唯品会/抖音/快手需要显式指定。
#    覆盖范围以响应里的 channels_covered 为准，不要从该参数推断。
source:
  0: 全部（实测仅含 1/2/3）
  1: 淘宝/天猫
  2: 京东
  3: 拼多多
  4: 苏宁
  5: 唯品会
  7: 抖音
  8: 快手
```

## 命令说明

### 搜索设备或耗材

```shell
# 主设备/整机
uv run scripts/main.py search --source=0 --keyword='惠普 M126a'
uv run scripts/main.py search --source=0 --keyword='戴尔 PowerEdge R750'
uv run scripts/main.py search --source=0 --keyword='联想 ThinkCentre M730q 台式机'
uv run scripts/main.py search --source=0 --keyword='A4 复印纸 70g'

# 配套耗材：设备型号 + 耗材类别词（详见下节）
uv run scripts/main.py search --source=0 --keyword='惠普 DeskJet 2720 墨盒'

# 指定渠道与页码
uv run scripts/main.py search --source=2 --keyword='奔图 P2200' --page=2
```

### 查询购买详情

```shell
uv run scripts/main.py detail --source={source} --id={goodsId}
```

解析注意：`detail` 返回的 JSON 中，`data.buy_url`（购买链接）与 `data.coupon_code`（淘口令）是核心字段。必须读取完整输出，严禁用 `tail` 或管道截断，否则可能导致链接遗失。

`data.goodsId` 是**回填的调用参数**（上游自己返回的 id 常与请求值不同，不要用它去二次查询）。
顶层 `invoice_data_available: false` 表示上游未提供开票资质字段——此时在回答里整列
说明一次"平台未提供开票信息"，不要逐行写"专票未标注"。

### 查询配套耗材（设备型号 + 耗材类别词）

查某台设备的耗材，**不需要先查映射表**，直接把设备型号和一个耗材类别词拼成关键词搜：

```shell
uv run scripts/main.py search --source=0 --keyword='惠普 DeskJet 2720 墨盒'
uv run scripts/main.py search --source=0 --keyword='奔图 P2200 碳粉盒'
uv run scripts/main.py search --source=0 --keyword='戴尔 PowerEdge R750 2.5寸 硬盘托架'
```

- **为什么带上设备型号**：电商标题普遍写成「适用 惠普 2720 墨盒」，带上机型能同时召回
  原装款与第三方兼容款，比只搜一个型号代码召回更全。
- **词序：`{设备} {类别词} {规格}`**。规格必须排在类别词**之后**——实测
  「戴尔 R750 服务器内存 ECC RDIMM」有 21 条，而把规格挪到类别词前面的
  「戴尔 R750 ECC RDIMM 服务器内存」只剩 **1 条**。
- **规格只用来看，不用来筛**：不要自行往关键词里加 `RDIMM`/`UDIMM`（上游会当精确
  筛选：8 台服务器同一批 base 从 246 条掉到 70 条）或容量/频率（`32G` 一加 40 条变
  25 条）。规格是否对口看每行的 `items[].attributes.spec_match`。
- **型号以搜索结果为准**：结果里的 `items[].attributes.supply_type`（`原装` /
  `第三方兼容`）与 `items[].attributes.model_code`（型号）就是这台机器实际在售的规格。
  **不要凭设备型号自行推测耗材型号，更不要拿推测出的型号去下单。**
- 已经知道原装型号时（机身标签 / 说明书）也可以直接用它搜：`--keyword='CC388A 碳粉盒'`。
  **不要加「原装」或「适用」后缀**：上游对这两个词不做筛选，加了只是白占一次查询。
  供应类型改看返回的 `items[].attributes.supply_type`。

类别词怎么选、`matched: false` 时怎么补搜，见 `references/answer-rules.md` 第 1 节。

### 查看云端未命中日志统计（运维用）

```shell
uv run scripts/main.py unmatched
```

### 交付前自检（强制）

回答渲染完成后，**必须**先把原文喂给自检脚本再交给用户。只要脚本退出码非 0，
说明购买链接没写进表格，必须按它给出的 `instruction` 重新生成，不得直接输出：

```shell
uv run scripts/check_answer.py --file /tmp/answer.md   # 回答已落盘（推荐）
cat /tmp/answer.md | uv run scripts/check_answer.py    # 从标准输入
```

退出码 `0` 通过，`1` 有必须修正项，`2` 输入有问题。产物为 JSON：`violations`
逐条给出违规行号与原因，`instruction` 是给宿主 AI 的重写指令。

## 云端服务配置

脚本默认连生产环境 `https://api.ninenode.com/api/office-buyer`。本机自测时指向本地服务
（服务端默认监听 `11443`）：

```shell
export OFFICE_BUYER_SERVER_URL='http://localhost:11443'
```

## 输出交付规则

回答前先 Read `references/answer-rules.md`，按规范格式进行 Markdown 渲染。核心要求：

- **先看 `matched` 字段**：为 `false` 时执行「宿主 AI 智能匹配兜底」——用「设备型号 + 耗材类别词」二次调用 `search`（见 `references/answer-rules.md` 第 1 节），再合并展示。不要凭设备型号推测耗材型号。
- **数据强绑定**：1 行表格 = 1 条原始记录，严禁跨记录拼凑价格、规格与链接。
- **必须将购买链接放在表格中**：链接的写法只占两个汉字宽度，不会撑坏表格——“表格太宽/链接太长”不构成省略该列的理由。严禁将链接放在表格外或回答末尾。
- **区分供应类型**：原装与第三方兼容耗材必须分栏或分行展示，不得混价比较。
- **纸品按单价横比**：复印纸/文具类必须折算到「¥/包」「¥/卷」「¥/支」再比较总价。
- **证据不足不臆断**：专票与质保只在有明确字段依据时标注，缺失写"未标注"。
- **必附数据来源声明**。
- **交付前跑自检**：`uv run scripts/check_answer.py`，退出码非 0 必须重新生成（见上）。

## 关于脚本

本技能提供的脚本不会写入本地文件，可放心使用。`scripts/main.py` 仅作为客户端请求云端服务，
以及由云端转请求三方比价平台的商品与价格数据；`scripts/check_answer.py`
是纯本地的格式自检器，只读取你传给它的回答文本（`--text` / stdin）或你指定的文件
（`--file`），不发起任何网络请求。
