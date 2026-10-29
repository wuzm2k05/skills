"""全品类识别、耗材/配件映射与未命中日志记录引擎。

职责：
1. 加载并缓存三张静态映射表（打印机耗材 / 服务器配件 / 纸品文具）。
2. 对用户关键词做归一化，并在映射表中做多级匹配（精确 → 别名 → 模糊）。
3. 命中时产出「主设备 + 原装耗材 + 兼容耗材 + 扩展配件」的查询计划。
4. 未命中时把关键词、时间戳、客户端 IP 追加写入 ``logs/unmatched.log``，
   并标记 ``matched=False``，由调用方回退为原始关键词直搜。
5. 对上游返回的商品标题做属性解析（原装/兼容、克重、包装规格），
   并对纸品/文具类商品计算折合单包/单支均价。
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from ..config import settings

# --------------------------------------------------------------------------- #
# 常量与规则表
# --------------------------------------------------------------------------- #

#: 原装耗材信号词
ORIGINAL_SIGNALS = ("原装", "原厂", "原装正品", "官方原装", "原装进口", "OEM原装")
#: 第三方兼容信号词
COMPATIBLE_SIGNALS = (
    "适用", "兼容", "通用", "代用", "国产", "第三方", "适配", "可替代", "平替",
)

#: 正式原厂前缀：厂商中文名 + 括号英文名的写法，如「惠普(HP)」「联想(Lenovo)」。
#: 只写「原装」二字的不算——电商标题里兼容品也大量这么写。用于 ``_supply_tier``。
OEM_PREFIXES = (
    "惠普(HP", "惠普（HP", "HP(", "联想(LENOVO", "联想（LENOVO", "LENOVO(",
    "兄弟(BROTHER", "兄弟（BROTHER", "BROTHER(", "佳能(CANON", "佳能（CANON",
    "CANON(", "爱普生(EPSON", "爱普生（EPSON", "EPSON(", "奔图(PANTUM",
    "奔图（PANTUM", "PANTUM(", "京瓷(KYOCERA", "京瓷（KYOCERA", "KYOCERA(",
    "戴尔(DELL", "戴尔（DELL", "DELL(", "理光(RICOH", "理光（RICOH",
    "RICOH(", "得力(DELI", "得力（DELI", "DELI(",
)

#: 品牌词库（用于标题解析与兜底）
BRANDS = (
    "惠普", "HP", "爱普生", "Epson", "佳能", "Canon", "兄弟", "Brother",
    "联想", "Lenovo", "戴尔", "Dell", "奔图", "Pantum", "理光", "Ricoh",
    "京瓷", "Kyocera", "富士施乐", "Fuji Xerox", "施乐", "Xerox",
    "柯尼卡美能达", "KONICA MINOLTA", "美能达", "三星", "Samsung",
    "得力", "Deli", "晨光", "M&G", "齐心", "Comix", "广博", "天章",
    "亚太森博", "APP", "金光", "UPM", "Double A", "达伯埃", "真彩",
    "浪潮", "Inspur", "华为", "Huawei", "H3C", "华三", "锐捷", "Ruijie",
    "海康威视", "HIKVISION", "大华", "Dahua", "Apple", "苹果",
    "金士顿", "Kingston", "三星内存", "海力士", "SK Hynix", "镁光", "Micron",
    "西部数据", "WD", "希捷", "Seagate", "东芝", "Toshiba", "致钛", "长江存储",
)

#: 纸品克重 / 幅面 / 包装规格抽取规则
PAPER_SIZE_RE = re.compile(r"\b(A[3-6]|B[4-5])\b", re.IGNORECASE)
PAPER_GRAM_RE = re.compile(r"(\d{2,3})\s*(?:g|G|克)", re.IGNORECASE)
#: 表示「件数」的量词：作为除数时必须是这些单位之一
COUNT_UNITS = ("包", "卷", "个", "支", "盒", "本", "条", "块", "只", "袋", "瓶", "副")
#: 复数量词（「5包装」「8包」「20包」）
MULTI_UNIT_RE = re.compile(r"(\d{1,3})\s*(" + "|".join(COUNT_UNITS) + r")\s*(?:装|/包)?")
#: 单包信号：明确表达「这一件就是包」
SINGLE_PACK_RE = re.compile(r"(单包|一包|1包|单包装|每包)")
#: 整箱信号
BOX_RE = re.compile(r"(整箱|箱装|一整箱|/\s*箱|每箱|一箱)")
#: 张数统计（「500张」「2500页」）
SHEET_COUNT_RE = re.compile(r"(\d{2,5})\s*(?:张|页|枚|片)")
#: 耗材型号：字母+数字组合，如 CC388A / PD-201 / TN-2325 / 88A。
#:
#: 不能使用 ``\b``：``\b`` 的边界定义是「词字符与非词字符之间」，而汉字在 Python
#: 的 re 里算词字符，所以「适用惠普CC388A碳粉」中 CC388A 左右都是词字符，``\b``
#: 不成立，整条正则匹配不到。实测该查询下 56 条商品里 17 条标题含 CC388A，
#: 旧写法只解析出 8 条——而 ``attributes.model_code`` 正是 answer-rules 让宿主 AI
#: 信任的型号来源。改用「右侧不是字母数字」的前瞻代替 ``\b``，汉字相邻即可命中。
#:
#: 第二分支 ``\d{2,3}[A-Z]{1,2}`` 覆盖「88A」「28A」这类以数字开头的短型号
#: （惠普按印量编号的碳粉盒），并要求右侧同样不是字母数字，避免把
#: 「24/6 订书钉」「DeskJet 2720」这类误判成型号。
MODEL_RE = re.compile(
    r"([A-Z]{1,4}-?\d{2,5}[A-Za-z]{0,2}(?![A-Za-z0-9])"
    r"|\d{2,3}[A-Z]{1,2}(?![A-Za-z0-9]))"
)

#: 换算策略 → 该品类的天然比价单位
UNIT_NAMES = {
    "calculate_per_pack_price": "包",
    "calculate_per_roll_price": "卷",
    "calculate_per_label_price": "张",
    "calculate_per_piece_price": "个",
}

# --------------------------------------------------------------------------- #
# 硬件规格 token（只用于「标注」，绝不用于「过滤」）
# --------------------------------------------------------------------------- #
#: 从 memory_spec / storage_spec 里抽出的可核对规格 token。
#: 刻意不抽容量（32G/64G/128G）：容量由用户需求决定，写进关键词反而把召回砍半
#: （实测「戴尔 R750 服务器内存 DDR4 ECC 32G」n=11，「…ECC」n=20）。
SPEC_TOKEN_RE = re.compile(
    r"(DDR[45]|LRDIMM|RDIMM|UDIMM|SO-?DIMM|ECC"
    r"|\d{3,4}\s*MHz"
    r"|[23]\.5\s*寸|M\.2|NVMe|SAS|SATA|PCIe\s*[345]\.0)",
    re.IGNORECASE,
)

#: 互斥规格族：同族内取值不同即为「冲突」。
#: 服务器内存实测 RDIMM 与 UDIMM 会在同一批结果里共现（「戴尔 R750 服务器内存
#: DDR4 ECC」20 条里 RDIMM 4 条、UDIMM 亦有），2.5 寸与 3.5 寸也共现（6/6）——
#: 所以任何按规格的**过滤**都会清空结果，只能标注冲突交给用户判断。
SPEC_FAMILIES: tuple[tuple[str, ...], ...] = (
    ("DDR4", "DDR5"),
    ("RDIMM", "UDIMM", "LRDIMM", "SODIMM"),
    ("2.5寸", "3.5寸"),
    ("SAS", "NVMe"),
)


def _norm_spec_token(token: str) -> str:
    """规格 token 归一化：去空格、大写，便于与标题比对。"""
    return re.sub(r"\s+", "", token).upper()


def spec_tokens_of(record: dict[str, Any]) -> list[str]:
    """由 memory_spec / storage_spec 文本抽取去重后的规格 token（保序）。

    这些 token 用于**标注**每行商品是否与设备规格相符（``spec_match``），
    因此保留频率（3200MHz）——它是核对点之一。
    """
    text = f"{record.get('memory_spec') or ''} {record.get('storage_spec') or ''}"
    seen: dict[str, None] = {}
    for hit in SPEC_TOKEN_RE.finditer(text):
        seen.setdefault(_norm_spec_token(hit.group(1)), None)
    return list(seen)


#: 写进关键词的规格 token 白名单——**只覆盖服务器内存，且只追加 `ECC` 一项**。
#:
#: 每一类被排除的 token 都有实测依据（8 台服务器 × 2 轮，sourceType=0 取小值）。
#: 同一批 base，逐级追加后缀后的合计条数：
#:
#: =================  ======
#: 追加的后缀          合计
#: =================  ======
#: （不追加）           260
#: `ECC`               246
#: `DDR4 ECC`          190
#: `DDR4 ECC RDIMM`     70
#: =================  ======
#:
#: - **形态词（RDIMM / UDIMM / LRDIMM / SO-DIMM）一律不追加**。上游把关键词当成
#:   精确筛选，加上去就归零：「浪潮 NF5280M5 服务器内存 ECC」41 条 → 加 RDIMM 后
#:   **0 条**、「DL380 Gen10 服务器内存 ECC」21 → 1、「联想 SR650 …」40 → 1。
#:   八台合计从 246 掉到 70。形态差异改由每行结果的 ``attributes.spec_match``
#:   标注（要 RDIMM 却写 UDIMM 会标 `conflict`），不做筛选。
#: - **代际（DDR4/DDR5）不追加**：加了同样压低召回（246 → 190，
#:   「浪潮 NF5280M5 … ECC」41 → 26、「联想 SR650 …」40 → 22），代际交给
#:   `spec_match` 标注。
#: - **频率（3200MHz）与容量（32G）不追加**：它们是用户按需挑的，写死等于替用户决定
#:   （「戴尔 R750 服务器内存」40 条 → 加 `32G` 25 条 → 加 `DDR4 ECC 32G` 14 条；
#:   加 `ECC RDIMM 3200MHz` 为 0 条）。
#: - **只对 ``category == "服务器"`` 追加**：服务器内存**必须** ECC，这是硬约束；
#:   台式机/笔记本/工作站（memory_spec 里可能写着「支持 ECC」）不是，且它们的召回基数
#:   本来就小（「联想 M730q 内存」5 条），再追加就归零了。
#:
#: `ECC` 本身**不是**净收益，而是召回与精度的交换：八台合计 260 条 → 246 条（−5%），
#: 但含 ECC 的行从 89 条升到 151 条（+70%）。服务器内存里挑错代际/形态的代价远小于
#: 挑到非 ECC 内存条，所以这笔交换划算。
#:
#: 盘位规格（2.5寸/3.5寸/SAS/NVMe）不自动追加，直接写在 ``accessory_keywords`` 里——
#: 不同盘位的托架物理上不通用：实测「戴尔 R750 硬盘托架 2.5寸」23 条（几乎全对口），
#: 去掉盘位是 54 条但混入大量 3.5 寸与其它机型。
KEYWORD_SPEC_RE = re.compile(r"ECC", re.IGNORECASE)


def keyword_spec_tokens(base: str, record: dict[str, Any]) -> list[str]:
    """按配件类别词挑出应当追加到关键词后的规格 token（去重、不与 base 重复）。

    只有服务器内存会拿到 ``["ECC"]``，其余一律返回空列表；被排除的 token 及理由
    见 ``KEYWORD_SPEC_RE`` 上方的注释。
    """
    if "内存" not in base:
        return []
    if str(record.get("category") or "").strip() != "服务器":
        return []
    source = record.get("memory_spec") or ""
    if "ECC" not in source.upper():
        return []
    existing = _norm_spec_token(base)
    tokens: list[str] = []
    for hit in KEYWORD_SPEC_RE.finditer(source):
        token = _norm_spec_token(hit.group(0))
        if token not in existing and token not in tokens:
            tokens.append(token)
    return tokens


def spec_match_of(title: str, want: list[str]) -> dict[str, Any]:
    """判断标题与设备规格的相符程度，**只标注不筛选**。

    返回 ``verdict`` 取 ``match`` / ``conflict`` / ``unknown``：

    - ``unknown``：设备本身无规格 token（显示器、网络设备），或标题未出现任何规格词。
    - ``conflict``：标题出现了同族的另一个取值（要 RDIMM 却写 UDIMM、要 DDR4 却写 DDR5、
      要 2.5 寸却写 3.5 寸）。这类商品**不剔除**——上游同批结果本来就混着两种规格，
      剔除会把结果清空；由 answer-rules 要求宿主 AI 提示用户核对。
    - ``match``：标题命中了至少一个所需 token，且无同族冲突。
    """
    if not want:
        return {"verdict": "unknown", "matched": [], "conflicts": []}
    squashed = _norm_spec_token(title)
    matched = [t for t in want if t in squashed]
    conflicts: list[str] = []
    for family in SPEC_FAMILIES:
        if not any(t in want for t in family):
            continue
        hit = [t for t in family if t in squashed]
        if hit and not any(t in matched for t in hit):
            conflicts.append(hit[0])
    if conflicts:
        verdict = "conflict"
    elif matched:
        verdict = "match"
    else:
        verdict = "unknown"
    return {"verdict": verdict, "matched": matched, "conflicts": conflicts}


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #
@dataclass
class RelatedQuery:
    """一条关联查询（耗材 / 配件 / 规格变体）。"""

    keyword: str
    kind: str  # original | compatible | accessory | variant
    label: str
    source: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "keyword": self.keyword,
            "kind": self.kind,
            "label": self.label,
            "source": self.source,
        }


@dataclass
class MatchResult:
    """关键词识别与映射结果。"""

    matched: bool
    keyword: str
    normalized: str
    category: str = "未知"
    matched_by: str = "none"  # consumables | server_hardware | paper_spec | none
    match_key: str | None = None
    device_name: str | None = None
    primary_keyword: str = ""
    related: list[RelatedQuery] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    unit_calculation: str | None = None
    unit_price_basis: dict[str, Any] = field(default_factory=dict)
    notes: str = ""

    def plan(self, max_related: int) -> list[dict[str, Any]]:
        """产出查询计划：第一条必然是主关键词。"""
        plan = [
            {
                "keyword": self.primary_keyword,
                "kind": "primary",
                "label": "主设备/主商品",
                "source": 0,
            }
        ]
        plan.extend(item.as_dict() for item in self.related[:max_related])
        return plan


# --------------------------------------------------------------------------- #
# 关键词归一化
# --------------------------------------------------------------------------- #
_NORMALIZE_DROP = (
    "的", "请问", "帮我", "查一下", "查询", "比价", "价格", "多少钱", "报价",
    "最低价", "哪里买", "采购", "想买", "购买", "推荐", "型号", "多少钱一台",
    "全网", "谢谢",
)


def normalize_keyword(keyword: str) -> str:
    """去噪、统一大小写与符号，供映射表检索使用。"""
    text = (keyword or "").strip()
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"[\s　]+", " ", text)
    for word in _NORMALIZE_DROP:
        text = text.replace(word, "")
    # 全角字母数字转半角
    text = "".join(
        chr(ord(ch) - 0xFEE0) if 0xFF01 <= ord(ch) <= 0xFF5E else ch for ch in text
    )
    return re.sub(r"\s+", " ", text).strip().strip("?？。,.，、")


def _squash(text: str) -> str:
    """去掉所有分隔符与空格，仅保留字母数字与汉字，用于宽松比对。"""
    return re.sub(r"[^0-9a-zA-Z一-鿿]", "", text).lower()


def _alias_in_keyword(alias: str, normalized: str) -> bool:
    """短别名（squash 后不足 4 字符）需以非字母数字为界出现在关键词里。

    「T27」是「惠普deskjet2720」的子串，纯子串匹配会把 ThinkVision T24 显示器的
    配件当成 DeskJet 2720 的耗材带回去。加长度下限也能挡住这一例，但会一并停用
    「24/6」「E15」「T27」这些长度恰为 3 的正常别名，所以改成看边界：左右任一侧
    是串首/串尾或非字母数字字符即算命中。
    """
    for match in re.finditer(re.escape(alias.strip()), normalized, re.IGNORECASE):
        start, end = match.start(), match.end()
        left_ok = start == 0 or not normalized[start - 1].isalnum()
        right_ok = end == len(normalized) or not normalized[end].isalnum()
        if left_ok or right_ok:
            return True
    return False


# --------------------------------------------------------------------------- #
# 映射表加载
# --------------------------------------------------------------------------- #
class MappingTables:
    """三张静态映射表的惰性加载与索引构建（线程安全，进程内缓存）。"""

    def __init__(self, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or settings.data_dir
        self._lock = threading.RLock()
        self._loaded = False
        self.consumables: dict[str, dict[str, Any]] = {}
        self.server_hardware: dict[str, dict[str, Any]] = {}
        self.paper_spec: dict[str, dict[str, Any]] = {}

    # -- 内部工具 ---------------------------------------------------------- #
    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as fp:
                raw = json.load(fp)
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:  # 数据文件写坏时不应拖垮服务
            print(f"[category_engine] 解析失败 {path}: {exc}")
            return {}
        # 剔除 _meta 描述块
        return {k: v for k, v in raw.items() if not k.startswith("_") and isinstance(v, dict)}

    def load(self, force: bool = False) -> None:
        with self._lock:
            if self._loaded and not force:
                return
            self.consumables = self._read_json(settings.consumables_file)
            self.server_hardware = self._read_json(settings.server_hardware_file)
            self.paper_spec = self._read_json(settings.paper_spec_file)
            self._loaded = True

    def reload(self) -> None:
        self.load(force=True)

    # -- 匹配 -------------------------------------------------------------- #
    def _iter_keys(self, table: dict[str, dict[str, Any]]) -> Iterable[tuple[str, dict[str, Any]]]:
        for key, value in table.items():
            yield key, value

    def find(self, normalized: str) -> tuple[str, str, str, dict[str, Any]] | None:
        """在全部映射表中检索。

        返回 ``(来源表名, 命中的键, 命中方式, 记录)``，未命中返回 ``None``。

        匹配优先级：键精确相等 → 键完全包含 → 别名精确 → 别名包含 → 模糊包含。
        """
        self.load()
        squash = _squash(normalized)
        if not squash:
            return None

        tables = (
            ("consumables", self.consumables, "打印机耗材映射"),
            ("server_hardware", self.server_hardware, "服务器/电脑配件映射"),
            ("paper_spec", self.paper_spec, "纸品文具规格映射"),
        )

        # 第一轮：键精确 / 键包含
        for table_name, table, _ in tables:
            for key, record in self._iter_keys(table):
                key_squash = _squash(key)
                if not key_squash:
                    continue
                if squash == key_squash:
                    return table_name, key, "key_exact", record
                if key_squash in squash:
                    return table_name, key, "key_substring", record

        # 第二轮：别名精确 / 别名包含
        for table_name, table, _ in tables:
            for key, record in self._iter_keys(table):
                for alias in record.get("aliases") or []:
                    alias_squash = _squash(str(alias))
                    if not alias_squash:
                        continue
                    if squash == alias_squash:
                        return table_name, key, "alias_exact", record
                    # 长别名沿用宽松子串；短别名必须带词边界，否则「T27」会被
                    # 「deskjet2720」整串吞进去
                    hit = (
                        alias_squash in squash
                        if len(alias_squash) >= 4
                        else _alias_in_keyword(str(alias), normalized)
                    )
                    if hit:
                        return table_name, key, "alias_substring", record

        # 第三轮：模糊包含（关键词被切碎时兜底，例如「惠普的M126a打印机多少钱」）
        for table_name, table, _ in tables:
            for key, record in self._iter_keys(table):
                candidates = [str(key)] + [str(a) for a in (record.get("aliases") or [])]
                for candidate in candidates:
                    candidate_squash = _squash(candidate)
                    if len(candidate_squash) >= 4 and candidate_squash in squash:
                        return table_name, key, "fuzzy", record
                    if len(squash) >= 4 and squash in candidate_squash:
                        return table_name, key, "fuzzy_reverse", record

        # 第四轮：纸品规格语义匹配（「A4纸70克」这类没有精确键的说法）
        paper = self._match_paper_by_attribute(normalized)
        if paper:
            key, record = paper
            return "paper_spec", key, "paper_attribute", record

        return None

    def _match_paper_by_attribute(
        self, normalized: str
    ) -> tuple[str, dict[str, Any]] | None:
        size_hit = PAPER_SIZE_RE.search(normalized)
        gram_hit = PAPER_GRAM_RE.search(normalized)
        if not size_hit:
            return None
        size = size_hit.group(1).upper()
        gram = int(gram_hit.group(1)) if gram_hit else 70
        if gram not in (70, 80):
            gram = 70
        wanted = f"{size}_{gram}g"
        record = self.paper_spec.get(wanted)
        if record:
            return wanted, record
        # 彩色纸兜底
        if "彩色" in normalized or "彩纸" in normalized:
            record = self.paper_spec.get(f"color_{size}_80g")
            if record:
                return f"color_{size}_80g", record
        return None


#: 进程内单例
TABLES = MappingTables()


# --------------------------------------------------------------------------- #
# 查询计划构建
# --------------------------------------------------------------------------- #
def _build_related_from_consumables(record: dict[str, Any]) -> list[RelatedQuery]:
    """把设备对应的耗材型号逐条转成查询。

    一条 ``model_keywords`` = 一种需要**单独比价**的耗材。鼓粉分离的机型（如
    DCP-7180DN 的 TN-2325 粉盒 + DR-2350 硒鼓）因此得到两条查询——旧表把两个
    型号塞进一个 ``model_code`` 字段（``"TN-2325 / DR-2350"``），硒鼓 DR-2350
    从来没被搜过。实测两者差一个价位档（各 3 轮中位价：粉盒 ¥32.4 / ¥90.0 /
    ¥35.9，硒鼓 ¥82.1 / ¥92.5 / ¥87.5），漏掉的正是更贵的那一半。

    关键词**不带**「原装 / 适用」后缀：上游对这两个词不做筛选。实测
    「惠普 88A 碳粉盒 原装」上游 52 条里 ``oem_likely`` 只有 2 条（¥529 / ¥1009），
    ``compatible`` 47 条（最低 ¥5.0、中位 ¥28.0）；「联想 LT2451 粉盒 原装」
    48 条里 ``oem_likely`` 4 条（¥279–365）、``compatible`` 28 条（¥12.6 起）。
    后缀白占一条 ``max_related_queries`` 配额，供应类型由结果行的
    ``attributes.supply_tier`` 判定，与查询词无关。
    """
    related: list[RelatedQuery] = []
    for kw in record.get("model_keywords") or []:
        keyword = str(kw).strip()
        if keyword:
            related.append(
                RelatedQuery(keyword=keyword, kind="consumable", label=keyword)
            )
    return related


def _build_related_from_hardware(record: dict[str, Any]) -> list[RelatedQuery]:
    """把设备对应的扩展配件转成查询，规格 token 一律**后置**且不含容量/频率。

    词序是上游的硬约束：``{设备} {类别词} {规格...}``。实测规格排在类别词**之后**
    才有结果：「戴尔 R750 服务器内存 ECC RDIMM」21 条，改写成「戴尔 R750 ECC RDIMM
    服务器内存」只剩 1 条（sourceType=2 上为 0）。

    配件词里的规格由 ``keyword_spec_tokens`` 从 ``memory_spec`` 派生，只追加
    ``ECC`` 一项且仅限服务器内存；形态词（RDIMM/UDIMM）、代际、频率与容量的取舍
    见 ``KEYWORD_SPEC_RE`` 上方的注释。
    """
    related: list[RelatedQuery] = []
    for kw in record.get("accessory_keywords") or []:
        base = str(kw).strip()
        if not base:
            continue
        tokens = keyword_spec_tokens(base, record)
        keyword = " ".join([base, *tokens]) if tokens else base
        related.append(RelatedQuery(keyword=keyword, kind="accessory", label=keyword))
    return related


def _build_related_from_paper(record: dict[str, Any]) -> list[RelatedQuery]:
    """把纸品的包装规格变体转成查询。

    这些变体查询有实测的增益：上游单条查询**按相关度排序**，整箱款与单包款的比例
    因查询词而异。每词 2 轮并集实测（含箱 / 含单包 的标题数）：

    ====================  =======  ======  ========
    查询                   并集条数   含箱    含单包
    ====================  =======  ======  ========
    A4 复印纸 70g            67       38      26
    A4 复印纸 70g 单包        60       36      51
    A4 复印纸 70g 5包箱装      26       22       3
    ====================  =======  ======  ========

    三条并起来 133 条（含箱 81 / 含单包 67），单包与整箱两种价格都有——宿主 AI
    需要这两种价格才能按折合单价横比，所以这两条查询保留。

    注意：上游对同一关键词的两次请求结果**大多数时候很稳**（7 个关键词 × 3 组
    实测 Jaccard 0.72–1.0，多数在 0.9 以上），但也出现过整批换一批的低谷，
    所以「这条查询有没有用」的判断仍应靠多轮并集而非单轮对比。
    """
    related: list[RelatedQuery] = []
    for kw in record.get("keywords") or []:
        keyword = str(kw).strip()
        if keyword:
            related.append(
                RelatedQuery(keyword=keyword, kind="variant", label="包装规格变体")
            )
    return related


def _primary_keyword(normalized: str, record: dict[str, Any]) -> str:
    """决定主查询关键词。

    用户关键词已经足够具体（含品牌或多词）时沿用它——上游对「惠普 M126a」这类
    短查询召回更好；只给了型号（如「M126a」「R750」）时补全为官方全称，
    否则上游容易召回同型号的配件而非整机。
    """
    official = record.get("device_name") or record.get("name") or normalized
    if len(_squash(normalized)) >= 6:
        return normalized
    return official


def build_match(keyword: str) -> MatchResult:
    """对用户关键词执行完整识别，产出可直接驱动查询的 MatchResult。"""
    normalized = normalize_keyword(keyword)
    result = MatchResult(
        matched=False,
        keyword=keyword,
        normalized=normalized,
        primary_keyword=normalized or keyword,
    )

    hit = TABLES.find(normalized)
    if not hit:
        result.attributes = parse_attributes(normalized)
        return result

    table_name, key, how, record = hit
    result.matched = True
    result.matched_by = table_name
    result.match_key = key
    result.device_name = record.get("device_name") or record.get("name")
    result.category = record.get("category") or _category_from_table(table_name)
    result.notes = record.get("notes", "")
    result.primary_keyword = _primary_keyword(normalized, record)

    if table_name == "consumables":
        result.related = _build_related_from_consumables(record)
        result.attributes = {
            "consumable_type": record.get("consumable_type"),
            "model_code": record.get("model_code"),
            # 每条关联查询对应的耗材型号，供 answer-rules 在「配套耗材」区块里
            # 标明这一行是粉盒还是硒鼓，而不是笼统写「耗材」。
            "model_keywords": list(record.get("model_keywords") or []),
        }
    elif table_name == "server_hardware":
        result.related = _build_related_from_hardware(record)
        result.attributes = {
            "memory_spec": record.get("memory_spec"),
            "storage_spec": record.get("storage_spec"),
            # 供 enrich_item() 给每条商品标注规格相符度（只标注，不筛选）
            "spec_tokens": spec_tokens_of(record),
        }
    else:  # paper_spec
        result.related = _build_related_from_paper(record)
        result.unit_calculation = record.get("unit_calculation")
        result.unit_price_basis = unit_price_basis(record)
        result.attributes = {
            "size": record.get("size"),
            "grain_weight": record.get("grain_weight"),
            "pack_specs": record.get("pack_specs"),
        }

    result.attributes["match_how"] = how
    return result


def _category_from_table(table_name: str) -> str:
    return {
        "consumables": "打印机耗材",
        "server_hardware": "电脑/服务器配件",
        "paper_spec": "纸品/办公文具",
    }.get(table_name, "未知")


# --------------------------------------------------------------------------- #
# 未命中日志
# --------------------------------------------------------------------------- #
class SizeRotatingLog:
    """按大小滚动的追加式文本日志。

    未命中关键词是持续增长的运维数据，且只会用于统计与补齐映射表，
    因此不引入额外依赖，用几十行实现「超限即滚动」：

        unmatched.log  →  unmatched.log.1  →  unmatched.log.2  →  ...
                          最旧的分片（.log.{backup_count}）被丢弃

    两个刻意的取舍：

    * **按整行写入**。一次 ``write()`` 写完一行，不会被 append 的原子性切断，
      因此读了半行的尿崩风险来自外部工具而非本服务。
    * **读取时自带兜底**。滚动过程中可能出现瞬间缺失，``read_*`` 遇到
      ``FileNotFoundError`` 一律当作「暂无日志」，不向上抛。

    多 worker 场景下每个进程各持一个实例、各自判断大小，滚动点可能不同步；
    日志只用于运维统计，这个精度足够。
    """

    def __init__(self, path: Path, max_bytes: int, backup_count: int) -> None:
        self.path = path
        self.max_bytes = max(0, max_bytes)
        self.backup_count = max(0, backup_count)
        self._lock = threading.Lock()

    def _rollover(self, incoming: int) -> None:
        """把 ``path`` 轮转为 ``path.1``，其余分片依次后移。"""
        if self.backup_count <= 0:
            # 不留历史：直接清空当前文件
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            return
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backup_count - 1, 0, -1):
            src = self.path.with_name(f"{self.path.name}.{index}")
            if src.exists():
                src.replace(self.path.with_name(f"{self.path.name}.{index + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    def append(self, line: str) -> None:
        """追加一行；滚动与写入失败都不抛出，只打印告警。

        写入失败（磁盘只读、配额耗尽等）不能影响主流程——接口仍须正常返回
        ``matched: false``，这是本服务对调用方的承诺。
        """
        record = line + "\n"
        size = len(record.encode("utf-8"))
        with self._lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                if self.max_bytes and self.path.exists():
                    if self.path.stat().st_size + size > self.max_bytes:
                        self._rollover(size)
                with self.path.open("a", encoding="utf-8") as fp:
                    fp.write(record)
            except OSError as exc:
                print(f"[category_engine] 写入未命中日志失败: {exc}")

    def read_lines(self, limit: int | None = None) -> list[str]:
        """按「先旧后新」的顺序读取当前分片与历史分片。

        滚动后新记录落在 ``path``、旧记录在编号更大的分片里，因此拼接顺序是
        ``.N … .1, path``，这样调用方取尾部即可得到最近记录。
        """
        chunks: list[list[str]] = []
        for path in reversed(self.all_paths()):  # 最旧的分片排在最前
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fp:
                    chunks.append([line.rstrip("\n") for line in fp if line.strip()])
            except OSError as exc:
                print(f"[category_engine] 读取未命中日志失败 {path}: {exc}")

        lines = [line for chunk in chunks for line in chunk]
        if limit is not None and limit > 0:
            return lines[-limit:]
        return lines

    def all_paths(self) -> list[Path]:
        """当前分片 + 全部历史分片，从新到旧。"""
        return [self.path] + [
            self.path.with_name(f"{self.path.name}.{index}")
            for index in range(1, self.backup_count + 1)
        ]

    def existing_paths(self) -> list[Path]:
        """已落盘的全部分片，从旧到新。"""
        return [p for p in reversed(self.all_paths()) if p.exists()]

    def rotated_paths(self) -> list[Path]:
        """现存的历史分片（不含当前分片），从旧到新。"""
        return [p for p in self.existing_paths() if p != self.path]


#: 进程内单例（延迟创建，便于测试替换 settings）
_UNMATCHED_LOG: SizeRotatingLog | None = None
_UNMATCHED_LOG_KEY: tuple[str, int, int] | None = None


def unmatched_log() -> SizeRotatingLog:
    """返回未命中日志写入器；配置变化时自动重建。"""
    global _UNMATCHED_LOG, _UNMATCHED_LOG_KEY
    key = (
        str(settings.unmatched_log),
        settings.log_max_bytes,
        settings.log_backup_count,
    )
    if _UNMATCHED_LOG is None or _UNMATCHED_LOG_KEY != key:
        _UNMATCHED_LOG = SizeRotatingLog(
            settings.unmatched_log, settings.log_max_bytes, settings.log_backup_count
        )
        _UNMATCHED_LOG_KEY = key
    return _UNMATCHED_LOG


def log_unmatched(keyword: str, client_ip: str = "-", extra: str = "") -> str:
    """把未匹配关键词追加写入 ``logs/unmatched.log``，返回写入的整行内容。

    行格式（与需求书一致）::

        [2026-10-26 12:00:00] [UNMATCHED] Keyword: 奔图 P2200 | IP: 127.0.0.1

    关键词按原文记录（不做归一化，便于按用户真实说法补齐映射表），但会截断到
    ``settings.unmatched_keyword_max_chars`` 个字符，保证单行大小有界。
    """
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    shown = keyword or ""
    limit = settings.unmatched_keyword_max_chars
    if limit > 0 and len(shown) > limit:
        shown = shown[:limit] + "…(截断)"
    line = f"[{timestamp}] [UNMATCHED] Keyword: {shown} | IP: {client_ip}"
    if extra:
        line += f" | {extra}"
    unmatched_log().append(line)
    return line


def read_unmatched(limit: int = 200) -> list[str]:
    """读取最近的未命中记录（含已滚动的历史分片），供运维排查使用。"""
    return unmatched_log().read_lines(limit=limit)


def unmatched_stats() -> dict[str, Any]:
    """统计未命中日志：总条数 + Top 关键词，用于评估映射表缺口。

    统计范围包含全部历史分片，因此映射表补齐的效果不会被滚动记录吃掉。
    """
    lines = read_unmatched(limit=0)
    counter: dict[str, int] = {}
    for line in lines:
        m = re.search(r"Keyword:\s*(.*?)\s*\|\s*IP:", line)
        if m:
            counter[m.group(1)] = counter.get(m.group(1), 0) + 1
    top = sorted(counter.items(), key=lambda kv: kv[1], reverse=True)[:20]
    return {
        "total": len(lines),
        "unique": len(counter),
        "top_keywords": [{"keyword": k, "count": v} for k, v in top],
        "log_path": str(settings.unmatched_log),
        "log_files": [str(p) for p in unmatched_log().existing_paths()],
    }


# --------------------------------------------------------------------------- #
# 商品属性解析与单价换算
# --------------------------------------------------------------------------- #
def parse_attributes(title: str) -> dict[str, Any]:
    """从商品标题解析品类相关属性（原装/兼容、品牌、纸品规格等）。"""
    text = title or ""
    attrs: dict[str, Any] = {}

    # 1. 原装 vs 兼容
    is_original = any(sig in text for sig in ORIGINAL_SIGNALS)
    is_compatible = any(sig in text for sig in COMPATIBLE_SIGNALS)
    if is_original and not is_compatible:
        attrs["supply_type"] = "原装"
    elif is_compatible and not is_original:
        attrs["supply_type"] = "第三方兼容"
    elif is_original and is_compatible:
        attrs["supply_type"] = "原装(标题含适用词)"
    else:
        attrs["supply_type"] = "未标注"

    # 2. 品牌
    for brand in BRANDS:
        if brand.lower() in text.lower():
            attrs["brand"] = brand
            break

    # 3. 纸品规格
    size = PAPER_SIZE_RE.search(text)
    if size:
        attrs["paper_size"] = size.group(1).upper()
    gram = PAPER_GRAM_RE.search(text)
    if gram:
        weight = int(gram.group(1))
        if 40 <= weight <= 400:
            attrs["gram_weight"] = weight

    # 4. 包装规格
    #    电商标题常把「单包 500 张」「整箱 5 包」「4包」写在同一行，因此这里只提取
    #    原始信号，由 compute_unit_price() 按保守优先序判定，避免用错除数。
    options: list[dict[str, Any]] = []
    for match in MULTI_UNIT_RE.finditer(text):
        count, unit = int(match.group(1)), match.group(2)
        if any(o["count"] == count and o["unit"] == unit for o in options):
            continue
        options.append({"count": count, "unit": unit})
    if options:
        attrs["pack_options"] = options
        attrs["pack_count"] = options[0]["count"]
        attrs["pack_unit"] = options[0]["unit"]
    sheets = SHEET_COUNT_RE.search(text)
    if sheets:
        attrs["sheet_count"] = int(sheets.group(1))
    if SINGLE_PACK_RE.search(text):
        attrs["single_pack_hint"] = True
    if BOX_RE.search(text):
        attrs["box_hint"] = True

    # 5. 耗材型号（字母+数字组合，如 CC388A / PD-201 / TN-2325）
    model = MODEL_RE.search(text)
    if model:
        attrs["model_code"] = model.group(1)

    # 6. 供应层级（旁路字段，**不取代** supply_type）
    #
    #    supply_type 只是「标题里出现了哪类词」的文本信号，会把 ¥43.9 的得力
    #    兼容品和 ¥51.3 的再生品标成「原装」——因为它们的标题里确实写了「原装」。
    #    真正的原装 CC388A 售价在 ¥300~1000。这里再给一层更保守的判断，并**只标注
    #    不重分类**：supply_type 的四个取值是 answer-rules 与测试依赖的契约，
    #    改它会连带改掉渲染规则；而 supply_tier 是新增旁路，宿主 AI 可用来提示
    #    「该行标称原装但价格显著低于同批原装，请核对」。
    attrs["supply_tier"] = _supply_tier(text, attrs.get("model_code"))
    attrs["supply_tier_note"] = _SUPPLY_TIER_NOTES[attrs["supply_tier"]]
    return attrs


#: supply_tier 取值 → 给宿主 AI 的说明
_SUPPLY_TIER_NOTES: dict[str, str] = {
    "oem_likely": "标题含原厂前缀（如「惠普(HP)」）且带型号，大概率为渠道原装正品",
    "compatible": "标题含适用/兼容/通用/代用/平替等词，为第三方兼容品",
    "unknown": "无法从标题判断供应层级，请按 supply_type 原始信号处理",
}


def _supply_tier(text: str, model_code: str | None) -> str:
    """更保守的供应层级判断（``oem_likely`` / ``compatible`` / ``unknown``）。

    判据故意苛刻：仅「标题里有『原装』」不足以判定原装，还要求同时具备
    ① 一个型号代码，② 一个正式原厂前缀（``惠普(HP``、``联想(Lenovo`` 这类
    厂商全称+括号英文名的写法）。电商标题里只写「原装」的兼容品极多，
    加上这两条后误判显著减少，代价是漏判——漏判落回 ``unknown`` 由
    ``supply_type`` 兜底，比误判成原装安全。
    """
    if any(sig in text for sig in COMPATIBLE_SIGNALS):
        return "compatible"
    if not model_code:
        return "unknown"
    if not any(sig in text for sig in ORIGINAL_SIGNALS):
        return "unknown"
    upper = text.upper()
    if any(prefix.upper() in upper for prefix in OEM_PREFIXES):
        return "oem_likely"
    return "unknown"


def unit_price_basis(record: dict[str, Any]) -> dict[str, Any]:
    """由规格表记录推导换算基准。"""
    sheets_per_pack = int(record.get("sheets_per_pack") or settings.default_sheets_per_pack)
    packs_per_box = int(record.get("packs_per_box") or settings.default_packs_per_box)
    return {
        "sheets_per_pack": sheets_per_pack,
        "packs_per_box": packs_per_box,
        "pieces_per_box": sheets_per_pack * packs_per_box,
    }


def compute_unit_price(
    price: float | None,
    matched_attrs: dict[str, Any],
    basis: dict[str, Any] | None,
    unit_calculation: str | None = None,
) -> dict[str, Any]:
    """折算单价，供跨规格横比。

    电商标题的包装信息经常自相矛盾（「单包 500 张 一包 批发 整箱 5 包装一箱」
    这类写法把单包与整箱同时塞进标题），因此这里**只做能站得住脚的除法**：
    宁可少给一个数并如实标注存疑，也不给一个看起来精确的错数。

    判定顺序（任一步命中即返回）::

        1. 件数唯一且 > 1
           - 同时声称「单包」而件数恰为整箱件数 → 信号冲突，标记 ambiguous
           - 否则总价 ÷ 件数
        2. 件数有多个互相冲突的取值 → 标记 ambiguous，列出各解读
        3. 张数为单包张数的整数倍 → 折合包数后相除（恰为整箱包数则标 box）
        4. 张数恰为单包张数 → 视为单包价
        5. 张数不足一包 → 按张折算
        6. 只有「整箱」字样 → 除以规格表的整箱件数
        7. 只有「单包/一包」字样 → 视为单件价

    无法确定时返回 ``basis="basis_unknown"``；信号冲突返回 ``basis="ambiguous"``
    并在 ``candidates`` 中列出各种解读，由客户端提示用户"规格表述冲突，
    请以下单页面为准"，**不得替用户挑一个数**。
    """
    out: dict[str, Any] = {
        "total_price": price,
        "unit_price": None,
        "unit": None,
        "basis": None,
        "basis_note": None,
        "unit_calculation": unit_calculation,
        "candidates": [],
    }
    if price is None or price <= 0:
        out["basis"] = "no_price"
        out["basis_note"] = "无有效价格"
        return out

    basis = basis or {}
    attrs = matched_attrs or {}
    sheets_per_pack = int(basis.get("sheets_per_pack") or 0)
    packs_per_box = int(basis.get("packs_per_box") or 0)
    pieces_per_box = int(basis.get("pieces_per_box") or 0)
    natural_unit = UNIT_NAMES.get(unit_calculation or "", "件")

    options = [
        o
        for o in (attrs.get("pack_options") or [])
        if isinstance(o.get("count"), int) and o["count"] > 1 and o.get("unit") in COUNT_UNITS
    ]
    sheet_count = attrs.get("sheet_count")
    has_single_hint = bool(attrs.get("single_pack_hint"))
    has_box_hint = bool(attrs.get("box_hint"))

    def done(unit_price: float, unit: str, kind: str, note: str) -> dict[str, Any]:
        out.update(unit_price=round(unit_price, 4), unit=unit, basis=kind, basis_note=note)
        return out

    def ambiguous(note: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
        out.update(basis="ambiguous", basis_note=note, candidates=candidates)
        return out

    # 1 / 2. 件数信号
    if options:
        distinct = {(o["count"], o["unit"]) for o in options}
        if len(distinct) > 1:
            return ambiguous(
                "标题出现多个互相冲突的包装数量（"
                + "、".join(f"{c}{u}" for c, u in sorted(distinct))
                + "），无法确定总价对应的规格",
                [
                    {
                        "basis": "multi_pack",
                        "unit": unit,
                        "unit_price": round(price / count, 4),
                        "note": f"若总价对应 {count}{unit}",
                    }
                    for count, unit in sorted(distinct)
                ],
            )

        count, unit = options[0]["count"], options[0]["unit"]
        if has_single_hint and packs_per_box and count == packs_per_box:
            return ambiguous(
                f"标题同时出现「单包」与「{count}{unit}（整箱）」两种表述，"
                "无法判断总价对应单包还是整箱",
                [
                    {"basis": "single_pack", "unit": unit, "unit_price": round(price, 4),
                     "note": f"若总价对应 1{unit}"},
                    {"basis": "box", "unit": unit, "unit_price": round(price / count, 4),
                     "note": f"若总价对应整箱 {count}{unit}"},
                ],
            )
        if has_box_hint and packs_per_box and count == packs_per_box:
            return done(price / count, unit, "box", f"按整箱 {count}{unit}折算")
        return done(price / count, unit, "multi_pack", f"按 {count}{unit}折算")

    # 3 / 4 / 5. 张数信号
    if sheet_count and sheets_per_pack:
        # 「单包 500 张 …… 整箱 2500 页」：单包与整箱两种口径同现，无法判断总价对应哪个
        if has_single_hint and has_box_hint:
            packs = sheet_count // sheets_per_pack if sheet_count >= sheets_per_pack else 1
            return ambiguous(
                f"标题同时出现「单包 {sheets_per_pack} 张」与「整箱」两种口径，"
                "无法判断总价对应单包还是整箱",
                [
                    {"basis": "single_pack", "unit": "包", "unit_price": round(price, 4),
                     "note": f"若总价对应单包 {sheets_per_pack} 张"},
                    *(
                        [{"basis": "box", "unit": "包",
                          "unit_price": round(price / packs_per_box, 4),
                          "note": f"若总价对应整箱 {packs_per_box} 包（{packs_per_box * sheets_per_pack} 张）"}]
                        if packs_per_box else []
                    ),
                ],
            )
        if sheet_count > sheets_per_pack and sheet_count % sheets_per_pack == 0:
            packs = sheet_count // sheets_per_pack
            if has_box_hint and packs_per_box and packs == packs_per_box:
                return done(price / packs, "包", "box", f"按整箱 {packs} 包折算")
            return done(price / packs, "包", "sheet_count", f"{sheet_count} 张折合 {packs} 包")
        if sheet_count == sheets_per_pack:
            return done(price, "包", "single_pack", f"按单包 {sheet_count} 张折算")
        if sheet_count < sheets_per_pack:
            return done(price / sheet_count, "张", "per_sheet", f"按 {sheet_count} 张折算（不足一包）")
        # 张数多于单包但不是整数倍 → 无法确定包数
        return ambiguous(
            f"标题标注 {sheet_count} 张，不是单包 {sheets_per_pack} 张的整数倍，无法折算单价",
            [],
        )

    # 6. 只有「整箱」字样
    if has_box_hint and pieces_per_box > 0:
        return done(
            price / pieces_per_box,
            natural_unit,
            "box",
            f"按整箱 {pieces_per_box} {natural_unit}折算",
        )

    # 7. 只有「单包 / 一包」字样
    if has_single_hint:
        return done(price, natural_unit, "single_pack", "按单件（单包/单卷）价")

    out["basis"] = "basis_unknown"
    out["basis_note"] = "标题未标注包装规格，仅展示总价"
    return out


def enrich_item(item: dict[str, Any], match: MatchResult | None) -> dict[str, Any]:
    """把属性解析与单价换算结果并入单条商品记录。

    只有纸品/文具类（``unit_calculation`` 非空）才做单价折算；整机与耗材类
    不存在「按包/卷折算」的比价需求，统一返回 ``unit_price=None``，避免输出噪声。

    价格本身不做任何改动；``unit_price.unit_price`` 为 ``None`` 时，
    客户端只能展示 ``total_price``，不得自行倒推。

    命中 server_hardware 时额外给出 ``attributes.spec_match``：把商品标题与该
    设备的规格 token（DDR4 / ECC / RDIMM / 2.5寸…）比对，标记 ``match`` /
    ``conflict`` / ``unknown``。**只标注、不过滤**——实测同批结果里 RDIMM 与
    UDIMM 共现、「戴尔 R750 服务器内存 DDR4 ECC」20 条里 2.5 寸与 3.5 寸
    6/6 共现，按规格过滤会把结果清空。
    """
    attrs = parse_attributes(item.get("title") or "")
    item = dict(item)

    want_specs = (match.attributes.get("spec_tokens") or []) if match else []
    if want_specs:
        attrs["spec_match"] = spec_match_of(item.get("title") or "", want_specs)

    item["attributes"] = attrs

    unit_calculation = match.unit_calculation if match else None
    if not unit_calculation:
        item["unit_price"] = None
        return item

    basis = match.unit_price_basis if match else None
    try:
        price = float(item["actualPrice"]) if item.get("actualPrice") is not None else None
    except (TypeError, ValueError):
        price = None

    item["unit_price"] = compute_unit_price(price, attrs, basis, unit_calculation)
    return item
