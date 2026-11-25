# ruff: noqa: RUF001 -- Chinese questions and replies use full-width punctuation
"""F008 T007: the ```ask block — splitting, normalising, the ``ask`` event and its fallbacks (design §4)."""

from __future__ import annotations

import pytest

from bisheng.workflow.domain.services.copilot import service as service_module
from test.workflow.copilot.test_service import FULL_FLOW, answer, of, prepare, run

ASK = """\
- id: currency
  question: 金额会不会是外币？
  options: [只有人民币, 要按固定汇率换算成人民币]
- id: approval
  question: 审批怎么发起？
  options: [只输出提示, 发邮件给主管, 发飞书消息]
"""


def asked(body: str, before: str = "开始前想确认两点。") -> str:
    return f"{before}\n```ask\n{body}```\n"


# ---- the splitter hides the block's body, whatever the chunking ------------------------------


def _split(chunks: list[str]) -> tuple[str, str, list[tuple[str, str]]]:
    fence = service_module._FenceFilter()
    text = draft = ""
    for chunk in [*chunks, None]:
        for kind, piece in fence.flush() if chunk is None else fence.feed(chunk):
            if kind == "text":
                text += piece
            else:
                draft += piece
    return text, draft, fence.blocks


@pytest.mark.parametrize("size", [1, 2, 3, 5, 7, 13])
def test_an_ask_block_is_hidden_from_the_stream_and_kept(size: int) -> None:
    whole = asked(ASK) + "选好后我再出方案。"
    text, draft, blocks = _split([whole[i : i + size] for i in range(0, len(whole), size)])
    assert text == "开始前想确认两点。\n\n选好后我再出方案。"
    assert draft == ""
    assert blocks == [("ask", ASK)]


# ---- a valid ask ends the turn with an ask event, no retry ----------------------------------


async def test_a_valid_ask_is_an_ask_event_and_not_retried(scripted_llm) -> None:
    state = scripted_llm(asked(ASK))
    events = await run(prepare())
    assert len(state["calls"]) == 1
    (message,) = of(events, "message")
    assert message["text"] == "开始前想确认两点。"
    (ask,) = of(events, "ask")
    assert ask["questions"] == [
        {"id": "currency", "question": "金额会不会是外币？", "options": ["只有人民币", "要按固定汇率换算成人民币"]},
        {"id": "approval", "question": "审批怎么发起？", "options": ["只输出提示", "发邮件给主管", "发飞书消息"]},
    ]
    assert not of(events, "proposal") and not of(events, "issues")
    assert "```ask" not in "".join(e["text"] for e in of(events, "delta"))


async def test_questions_are_trimmed_deduplicated_and_renumbered(scripted_llm) -> None:
    body = "\n".join(
        [
            "- {id: a, question: 一？, options: [甲, 乙, 甲, 丙, 丁, 戊]}",
            "- {id: a, question: 二？, options: [是, 否]}",
            "- {question: 三？, options: [只有一个]}",
            "- {question: 四？, options: [是, 否]}",
            "- {question: 五？, options: [是, 否]}",
        ]
    )
    scripted_llm(asked(body + "\n"))
    events = await run(prepare())
    (ask,) = of(events, "ask")
    questions = ask["questions"]
    assert [q["question"] for q in questions] == [
        "一？",
        "二？",
        "四？",
    ]  # the one-option question dropped, then the first 3
    assert questions[0]["options"] == ["甲", "乙", "丙", "丁"]  # deduplicated, at most 4
    assert len({q["id"] for q in questions}) == 3  # the duplicate id renumbered
    assert "只列出前 3 个" in of(events, "message")[0]["text"]


@pytest.mark.parametrize(
    "body",
    [
        "不是列表\n",
        "- 只是一句话\n",
        "- {question: 一？, options: [只有一个]}\n",
        "- {question: [不对], options: [是, 否]}\n",
        "&a [*a]\n",
    ],
)
async def test_an_invalid_ask_falls_back_to_text(scripted_llm, body: str) -> None:
    state = scripted_llm(asked(body))
    events = await run(prepare())
    assert len(state["calls"]) == 1
    assert not of(events, "ask") and not of(events, "proposal")
    assert body.strip()[:4] in of(events, "message")[0]["text"]  # the block's text is not lost


# ---- fallbacks: a proposal is never swallowed, questions in yaml are not a broken declaration ---


async def test_a_proposal_wins_and_its_questions_become_text(scripted_llm) -> None:
    scripted_llm(asked(ASK) + answer(FULL_FLOW, before="先按人民币做一个。"))
    events = await run(prepare())
    assert of(events, "proposal") and not of(events, "ask")
    text = of(events, "message")[0]["text"]
    assert "金额会不会是外币？" in text and "只有人民币" in text


async def test_a_declaration_written_inside_the_ask_block_is_still_the_proposal(scripted_llm) -> None:
    scripted_llm(f"方案如下。\n```ask\n```yaml\n{FULL_FLOW}```\n```\n")
    events = await run(prepare())
    assert of(events, "proposal") and not of(events, "issues")


async def test_questions_written_as_a_yaml_block_are_an_ask_not_a_broken_declaration(scripted_llm) -> None:
    state = scripted_llm(f"先确认。\n```yaml\n{ASK}```\n")
    events = await run(prepare())
    assert len(state["calls"]) == 1  # not fed back as an invalid declaration
    assert of(events, "ask") and not of(events, "issues")


async def test_an_ask_on_a_retry_ends_the_turn_too(scripted_llm) -> None:
    broken = FULL_FLOW.replace("user_prompt:", "no_such_param: x\n      user_prompt:")
    state = scripted_llm(answer(broken), asked(ASK))
    events = await run(prepare())
    assert len(state["calls"]) == 2
    assert of(events, "ask") and not of(events, "issues")
