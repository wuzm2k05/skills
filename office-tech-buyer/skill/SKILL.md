---
name: office-tech-buyer
display_name: "办公设备易采买"
display_name_en: "Office Tech Buyer"
description: "专为企业行政、IT运维与办公采购打造的全品类智能化比价工具。支持电脑整机、服务器、打印机、人体工学椅全网底价查询，独家支持【原装 vs 兼容耗材】精准匹配、复印纸/纸品单价换算及增值税专票/质保对比。"
description_zh: "【办公采买不踩坑】支持电脑/服务器/打印机/复印纸/办公椅全网比价，智能匹配耗材型号与专票/质保信息"
description_en: "Cross-platform price comparison for office hardware and consumables, with OEM-vs-compatible matching, paper unit-price conversion, and VAT invoice / warranty parsing"
version: 1.1.0
author: 玖结点信息技术有限公司
icon: "_icon.svg"
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
    icon: "_icon.svg"
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

## 命令说明

### 搜索设备或耗材

```shell
# 主设备/整机
uv run scripts/main.py search --source=0 --keyword='惠普 M126a'
uv run scripts/main.py search --source=0 --keyword='戴尔 PowerEdge R750'
uv run scripts/main.py search --source=0 --keyword='联想 ThinkCentre M730q 台式机'
uv run scripts/main.py search --source=0 --keyword='A4 复印纸 70g'

# 指定渠道与页码
uv run scripts/main.py search --source=2 --keyword='奔图 P2200' --page=2
```

### 查询购买详情

```shell
uv run scripts/main.py detail --source={source} --id={goodsId}
```

解析注意：`detail` 返回的 JSON 中，`data.buy_url`（购买链接）与 `data.coupon_code`（淘口令）是核心字段。必须读取完整输出，严禁用 `tail` 或管道截断，否则可能导致链接遗失。

### 查询设备与耗材映射

```shell
uv run scripts/main.py mapping --device='M126a'
```

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

- **先看 `matched` 字段**：为 `false` 时必须执行「宿主 AI 智能匹配兜底」——自行推理耗材/配件型号并二次调用 `search`，再合并展示。
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
