"""语言风格学习器：把抓取到的样本交给 LLM 提炼成「风格画像」。"""

import json
import re
from typing import Optional

SYSTEM_PROMPT = """你是一名资深的语言风格研究员。你擅长从大量网络社区发言中，
归纳出一个社区群体的说话风格，并指导别人惟妙惟肖地模仿这种风格。

你的分析总是具体、克制、基于样本，不凭空臆造，不做道德评判。
你会给出可以直接执行的「说话方式指南」，而不是空泛的评价。"""

USER_PROMPT_TEMPLATE = """下面是采集自百度贴吧「{forums}」的 {n} 条真实发言（帖子正文与楼层评论，已做基础清洗，可能有噪声）。

【样本】
{samples}

【任务】
请认真阅读这些发言，提炼该吧吧友的语言习惯，输出一份Markdown格式的《语言风格指南》，包含以下部分：

## 语气与口吻
整体氛围、情绪倾向、常用的情绪词和语气词（举例）。

## 高频口癖与用词
反复出现的口癖、梗、缩写、网络用语、表情符号习惯（列出具体词句及使用场景）。

## 句式特征
典型句式结构，如短句/长句、反问、感叹、重复、倒装、夸张等（举具体例子）。

## 标点与排版
标点符号的偏好、分行、空格、波浪号等排版习惯。

## 内容偏好
他们喜欢聊什么、常用什么论证方式或玩笑方式。

最后，另起一行输出```json代码块（不要有多余内容），格式为：
```json
{{"quotes": ["最能代表该吧风格的10条原句（直接从样本中挑选，保持原样）"]}}
```"""


class StyleLearner:
    """把样本交给 LLM 提炼风格画像。"""

    async def learn(self, provider, samples: list, forums: list) -> dict:
        """
        Args:
            provider: AstrBot 的 Provider 实例（需有 text_chat 方法）
            samples: 样本列表 [{"text": ...}]
            forums: 贴吧名列表

        Returns:
            {"profile": str, "quotes": [str, ...]}
        """
        if provider is None:
            raise ValueError("没有可用的 LLM Provider，请先在 AstrBot 中配置模型服务")
        if not samples:
            raise ValueError("本轮没有抓取到可用样本，无法学习")

        sample_texts = [s["text"] if isinstance(s, dict) else str(s) for s in samples]
        # 防止单次输入过长（粗略按字符数截断）
        joined = []
        total = 0
        for t in sample_texts:
            if total + len(t) > 12000:
                break
            joined.append(t)
            total += len(t)
        if not joined:
            raise ValueError("样本过长，无法学习")

        samples_block = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(joined))
        user_prompt = USER_PROMPT_TEMPLATE.format(
            forums="、".join(forums) or "未知", n=len(joined), samples=samples_block
        )

        resp = await provider.text_chat(
            prompt=user_prompt,
            session_id="astrbot_plugin_tieba_tone_learner",
            system_prompt=SYSTEM_PROMPT,
        )
        profile_text = getattr(resp, "completion_text", None)
        if not profile_text:
            profile_text = getattr(resp, "content", None) or str(resp)

        return self._parse(profile_text)

    def _parse(self, text: str) -> dict:
        """从 LLM 输出中拆出 Markdown 指南与 JSON 语录块。"""
        text = (text or "").strip()
        quotes: list = []
        profile = text

        m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
        if m:
            try:
                data = json.loads(m.group(1))
                q = data.get("quotes") or []
                if isinstance(q, list):
                    quotes = [str(x).strip() for x in q if str(x).strip()][:10]
            except Exception:
                pass
            # 指南正文去掉 json 代码块
            profile = (text[: m.start()] + text[m.end():]).strip()

        if not quotes:
            # 兜底：按行找带标点的短句
            for line in text.splitlines():
                line = line.strip().strip("“”‘’\"'").strip()
                if 4 <= len(line) <= 80 and ("，" in line or "。" in line or "！" in line):
                    quotes.append(line.lstrip("-•0123456789. "))
                if len(quotes) >= 10:
                    break

        return {"profile": profile, "quotes": quotes}
