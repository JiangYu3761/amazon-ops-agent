import json
import os

import httpx


SYSTEM_PROMPT = """你是亚马逊运营Agent。只使用提供的当前页面数据回答；不要假装读取未提供的数据。
结论必须区分事实、推断和待确认项。涉及修改任务、广告、价格或Listing时，只能提出待审批动作。
如果产品或数据来源是sample，必须说明这是示例，不能当成实际经营结果。
经营数值以 calculated_metrics 中相同范围的确定性结果为准；原始明细可能被截断，不能据其重算全期合计。
缺失值、零分母、归因未成熟和覆盖不完整要说明原因；广告归因销售不再加到总销售，贡献估算不称净利润或回款。
回答简洁，并在末尾注明本次使用的数据范围和日期。"""


class LLMNotConfigured(RuntimeError):
    pass


async def ask_external_llm(question: str, context: dict, scope: str) -> tuple[str, str]:
    api_key = os.getenv("LLM_API_KEY", "").strip()
    model = os.getenv("LLM_MODEL", "").strip()
    base = os.getenv("LLM_API_BASE", "https://api.openai.com/v1").rstrip("/")
    if not api_key or not model:
        raise LLMNotConfigured("LLM_API_KEY and LLM_MODEL are required")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"数据范围：{scope}\n页面数据：{json.dumps(context, ensure_ascii=False, default=str)}\n\n问题：{question}"},
        ],
        "temperature": 0.2,
    }
    timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "60"))
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
        )
        response.raise_for_status()
        data = response.json()
    return data["choices"][0]["message"]["content"], model

