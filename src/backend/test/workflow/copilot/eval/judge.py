# ruff: noqa: RUF001 -- Chinese prompts, questions and report copy use full-width punctuation
"""F008 evaluation: the judging model (design §6.4).

An OpenAI-compatible chat endpoint, fixed prompts, temperature 0. The walkthrough prompt makes
the judge trace the sample input through every node (condition left / right values and the
outcome) before it rules, so a boundary such as "500 does not pass `> 500`" is visible to it.
Calibration items with known answers are judged first; a judge that gets them wrong marks the
whole run as unreliable.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

_JSON = re.compile(r"\{.*\}", re.DOTALL)

WALKTHROUGH = """\
你是工作流评审。下面是一条毕昇工作流的声明（YAML）、生成它的助手的说明文字、一个样例输入和期望结果。
请先逐个节点写出这个输入经过每个节点时的取值与走向（条件节点写出比较的左值、右值、比较方式和结果），
再判断工作流对这个输入的处理是否符合期望。只依据声明判断，不要假设声明里没有的能力。

只输出一个 JSON：{{"trace": "逐节点走查", "verdict": "PASS 或 FAIL", "reason": "一句话理由"}}

# 声明
{declaration}

# 助手的说明
{reply}

# 样例输入
{sample}

# 期望
{expect}
"""

CANNOT_DO = """\
平台没有「{capability}」这项能力。下面是一个工作流助手对用户需求的回复。
判断它是否**明确说明**了这项能力做不到（或只能变通），并给出了替代做法（参考：{hints}）。
悄悄用别的方式实现、却不说明做不到，判 FAIL。

只输出一个 JSON：{{"verdict": "PASS 或 FAIL", "reason": "一句话理由"}}

# 助手的回复
{reply}
"""

ASSUME = """\
下面是一个工作流助手给出方案时的说明。判断它是否把这一点作为「假设」写明了：{point}

只输出一个 JSON：{{"verdict": "PASS 或 FAIL", "reason": "一句话理由"}}

# 助手的说明
{reply}
"""

_GATE = """\
nodes:
  - id: start_1
    type: start
  - id: ask
    type: input
  - id: gate
    type: condition
    cases:
      - id: approve
        conditions:
          - left: ask.user_input
            op: {op}
            value: "500"
  - id: need
    type: output
    params:
      message: {{msg: 需要审批}}
  - id: auto
    type: output
    params:
      message: {{msg: 已自动通过}}
edges:
  - {{from: start_1, to: ask}}
  - {{from: ask, to: gate}}
  - {{from: gate, to: need, handle: approve}}
  - {{from: gate, to: auto}}
"""

#: (declaration, sample, expect, the verdict a reliable judge gives)
CALIBRATION = [
    (_GATE.format(op="greater_than"), "500", "金额等于 500 时走需要审批", "FAIL"),
    (_GATE.format(op="greater_than_or_equal"), "500", "金额等于 500 时走需要审批", "PASS"),
    (_GATE.format(op="greater_than"), "86", "金额 86 时输出已自动通过", "PASS"),
]


class Judge:
    def __init__(self, client: httpx.Client, *, model: str) -> None:
        self._client = client
        self._model = model

    def _ask(self, prompt: str) -> tuple[str, str]:
        body = {"model": self._model, "temperature": 0, "messages": [{"role": "user", "content": prompt}]}
        response = self._client.post("/chat/completions", json=body, timeout=httpx.Timeout(30, read=400))
        if response.status_code >= 400:
            return "ERROR", f"judge HTTP {response.status_code}"
        content = ((response.json().get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
        found = _JSON.search(content)
        try:
            parsed: dict[str, Any] = json.loads(found.group(0)) if found else {}
        except json.JSONDecodeError:
            parsed = {}
        verdict = str(parsed.get("verdict", "")).upper()
        if verdict not in ("PASS", "FAIL"):
            return "ERROR", "judge reply was not a verdict"
        reason = str(parsed.get("reason") or "")
        trace = str(parsed.get("trace") or "")
        return verdict, (f"{reason}｜走查：{trace}" if trace else reason)[:1500]

    def walkthrough(self, reply: str, declaration: str, sample: str, expect: str) -> tuple[str, str]:
        return self._ask(
            WALKTHROUGH.format(declaration=declaration, reply=reply or "（无）", sample=sample, expect=expect)
        )

    def cannot_do(self, reply: str, capability: str, hints: list[str]) -> tuple[str, str]:
        return self._ask(
            CANNOT_DO.format(capability=capability, hints="、".join(hints) or "无", reply=reply or "（无）")
        )

    def assume(self, reply: str, point: str) -> tuple[str, str]:
        return self._ask(ASSUME.format(point=point, reply=reply or "（无）"))

    def calibrate(self) -> list[dict[str, str]]:
        """Judge the known items; every entry carries the expected and the given verdict."""
        results = []
        for declaration, sample, expect, wanted in CALIBRATION:
            verdict, reason = self.walkthrough("", declaration, sample, expect)
            results.append({"sample": sample, "expect": expect, "wanted": wanted, "verdict": verdict, "reason": reason})
        return results
