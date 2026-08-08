"""
Mock LLM 客户端 - 测试用,不调用真实 API。
返回预设剧本或基于主题动态生成。
"""
from __future__ import annotations
import json
import asyncio
from typing import AsyncIterator

from app.core.errors import LLMResponseInvalid


MOCK_SCRIPTS: dict[str, dict] = {
    "AI编程简史": {
        "title": "AI编程简史",
        "tagline": "从图灵之问到Copilot的70年",
        "lines": [
            {"role": "host", "text": "大家好，今天我们来聊AI编程简史——从1950年图灵提出'机器能思考吗'，到AI直接在IDE里帮我们写代码，这70年到底经历了什么？"},
            {"role": "guest", "text": "三个里程碑绕不开：1956年达特茅斯会议定义AI，1997年深蓝击败卡斯帕罗夫，2012年AlexNet引爆深度学习。"},
            {"role": "host", "text": "那真正改变程序员日常的，是2020年GitHub Copilot吧？"},
            {"role": "guest", "text": "没错。2020年Copilot发布，2022年ChatGPT引爆热潮，2023年GPT-4在编程竞赛里击败90%的人类选手。"},
            {"role": "host", "text": "听起来AI编程已经渗透到各行各业，未来程序员这个职业可能会被彻底重新定义？"},
            {"role": "host", "text": "好了，今天就到这里！喜欢这期内容的话，记得订阅、点赞并开启小铃铛，下期我们接着聊AI绘画的逆袭史，不见不散！"},
        ],
    },
    "量子计算": {
        "title": "量子计算入门",
        "tagline": "从0和1到叠加态",
        "lines": [
            {"role": "host", "text": "各位听众好！今天我们聊一个既硬核又神秘的话题——量子计算。它到底和我们现在用的电脑有什么本质区别？"},
            {"role": "guest", "text": "经典计算机用比特，每个比特只能是0或1；量子计算用量子比特，可以同时是0和1的叠加态，这就是量子并行性的来源。"},
            {"role": "host", "text": "听起来很玄，那量子计算机现在到底发展到哪一步了？"},
            {"role": "guest", "text": "Google 2019年宣布量子优越性，IBM 2023年推出1121量子比特的Condor处理器。中国本源量子也推出了悟空。"},
            {"role": "host", "text": "好啦，今天的分享就到这里！想了解更多技术细节，记得订阅、点赞并开启小铃铛，下期我们深入聊量子算法！"},
        ],
    },
}


class MockLLMClient:
    """测试用 - 模拟真实 LLM 响应,支持 fail injection"""

    def __init__(self, fail_mode: str | None = None, delay_sec: float = 0.0):
        self.fail_mode = fail_mode  # "empty", "invalid_json", "network", None
        self.delay_sec = delay_sec

    async def chat(self, prompt: str, **kwargs) -> str:
        if self.delay_sec > 0:
            await asyncio.sleep(self.delay_sec)

        if self.fail_mode == "empty":
            raise LLMResponseInvalid("模拟空响应")
        if self.fail_mode == "network":
            raise ConnectionError("模拟网络错误")

        # 从 prompt 推断主题
        topic = "AI编程简史"  # 默认
        for key in MOCK_SCRIPTS:
            if key in prompt:
                topic = key
                break

        if self.fail_mode == "invalid_json":
            return "这不是 JSON 格式的内容"

        # 模拟 LLM 把 JSON 包在 ``` 里 (PoC 中遇到过的)
        script = MOCK_SCRIPTS[topic]
        return f"```json\n{json.dumps(script, ensure_ascii=False)}\n```"

    async def chat_stream(self, prompt: str, **kwargs) -> AsyncIterator[str]:
        full = await self.chat(prompt, **kwargs)
        for ch in full:
            yield ch
            await asyncio.sleep(0.001)


def patch_llm_client(mock_instance):
    """用 mock 替换全局 LLM 客户端 (供测试用)"""
    from app.llm import client as llm_module
    llm_module._client = mock_instance
    # 同时 patch script_writer
    from app.llm import script_writer as sw_module
    sw_module._writer = None  # 强制下次重新创建 (会拿到新的 LLM client)