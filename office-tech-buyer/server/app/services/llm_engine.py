"""LLM 智能化运维引擎。

职责：
1. 分析未命中日志中的高频关键词。
2. 结合上游搜索结果，利用 LLM 提取机型与耗材/配件的映射关系。
3. 动态生成或更新静态映射表 JSON 文件。
"""

from __future__ import annotations

import json
import logging
import asyncio
from pathlib import Path
from typing import Any, List, Dict

import httpx
from ..config import settings
from .category_engine import unmatched_stats, TABLES
from .maishou88 import maishou

logger = logging.getLogger(__name__)

class LLMEngine:
    """智能化运维核心类。"""

    def __init__(self) -> None:
        self.api_key = settings.llm_api_key
        self.base_url = settings.llm_base_url
        self.model = settings.llm_model
        self.enabled = settings.llm_enabled

    async def analyze_and_update(self) -> Dict[str, Any]:
        """执行一次完整的未命中分析与补全流程。"""
        if not self.enabled or not self.api_key:
            return {"status": "disabled", "message": "LLM 智能化运维未开启或未配置 API Key"}

        stats = unmatched_stats()
        top_keywords = stats.get("top_keywords", [])
        if not top_keywords:
            return {"status": "no_work", "message": "暂无未命中日志需要分析"}

        results = []
        for item in top_keywords[:5]:  # 每次分析前 5 个高频词
            keyword = item["keyword"]
            res = await self._process_keyword(keyword)
            results.append(res)

        # 只要有更新，就热重载映射表
        if any(r.get("updated") for r in results):
            TABLES.reload()

        return {
            "status": "completed",
            "processed_count": len(results),
            "details": results
        }

    async def _process_keyword(self, keyword: str) -> Dict[str, Any]:
        """针对单个关键词进行搜索分析与补全。"""
        # 1. 获取搜索结果作为上下文
        search_results = await maishou.search(keyword, source=0, page=1)
        if not search_results:
            return {"keyword": keyword, "updated": False, "reason": "无搜索结果，无法分析"}

        # 2. 调用 LLM 提取映射关系
        prompt = self._build_prompt(keyword, search_results)
        suggestion = await self._call_llm(prompt)
        
        if not suggestion or "mapping" not in suggestion:
            return {"keyword": keyword, "updated": False, "reason": "LLM 未能给出有效建议"}

        # 3. 更新对应的 JSON 文件
        mapping = suggestion["mapping"]
        table_type = suggestion.get("table_type") # consumables | server_hardware | paper_spec
        
        success = self._update_table(table_type, mapping)
        return {"keyword": keyword, "updated": success, "table": table_type}

    def _build_prompt(self, keyword: str, search_results: List[Dict[str, Any]]) -> str:
        # 简化搜索结果供 LLM 参考
        context = []
        for r in search_results[:10]:
            context.append({
                "title": r.get("title"),
                "shop": r.get("shopName"),
                "price": r.get("actualPrice")
            })

        return f"""你是一个办公设备专家。用户搜索了关键词 "{keyword}" 但我们的数据库未命中。
以下是该关键词在电商平台的真实搜索结果：
{json.dumps(context, ensure_ascii=False, indent=2)}

请分析这些结果，提取该设备及其配套耗材/配件的映射信息。
要求：
1. 识别设备类型（打印机/服务器/纸品）。
2. 提取设备全名 (device_name) 和 核心型号 (key)。
3. 提取配套耗材型号 (model_code) 和 搜索关键词 (model_keywords)。
4. 如果是服务器，提取内存/硬盘规格。

输出格式必须为 JSON：
{{
  "table_type": "consumables" | "server_hardware" | "paper_spec",
  "mapping": {{
    "key": "型号核心串",
    "value": {{
       "name": "设备全称",
       "category": "品类",
       "aliases": ["别名1", "别名2"],
       ...其他字段参考现有 schema
    }}
  }}
}}
"""

    async def _call_llm(self, prompt: str) -> Dict[str, Any] | None:
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"}
        }
        
        try:
            async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
                resp = await client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                return json.loads(content)
        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return None

    def _update_table(self, table_type: str, mapping: Dict[str, Any]) -> bool:
        file_map = {
            "consumables": settings.consumables_file,
            "server_hardware": settings.server_hardware_file,
            "paper_spec": settings.paper_spec_file
        }
        file_path = file_map.get(table_type)
        if not file_path or not file_path.exists():
            return False

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            key = mapping["key"]
            value = mapping["value"]
            
            # 简单的合并逻辑：如果 key 已存在，则合并 aliases
            if key in data:
                existing = data[key]
                new_aliases = set(existing.get("aliases", [])) | set(value.get("aliases", []))
                existing["aliases"] = list(new_aliases)
            else:
                data[key] = value
            
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return True
        except Exception as e:
            logger.error(f"更新映射表失败: {e}")
            return False

llm_engine = LLMEngine()
