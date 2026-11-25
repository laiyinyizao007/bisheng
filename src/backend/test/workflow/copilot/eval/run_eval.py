# ruff: noqa: RUF001 -- Chinese prompts, questions and report copy use full-width punctuation
"""F008 evaluation runner (design §6). Offline and manual: calls a real platform and model.

    cd src/backend
    python -m test.workflow.copilot.eval.run_eval --label baseline [--repeat 3] [--jobs 3] [--only id,...]

Environment (never written to disk or printed):
    BISHENG_EVAL_BASE        platform address, e.g. http://127.0.0.1:17860
    BISHENG_EVAL_TOKEN       a logged-in session token (the ``access_token_cookie`` value)
    BISHENG_EVAL_TENANT      optional management tenant header for a global super admin
    BISHENG_EVAL_JUDGE_BASE / _KEY / _MODEL   the judging model (OpenAI-compatible)
"""

from __future__ import annotations

import argparse
import json
import os
import secrets as _secrets
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from bisheng_flow_dsl.compiler import compile_spec
from bisheng_flow_dsl.decompile import decompile
from bisheng_flow_dsl.dump import dump
from test.workflow.copilot.eval import checks
from test.workflow.copilot.eval.bisheng_api import BishengApi, Turn
from test.workflow.copilot.eval.cases import HERE, load_cases, load_fixture
from test.workflow.copilot.eval.judge import CALIBRATION, Judge

RESULTS_DIR = HERE / "results"
MAX_CLARIFY_ROUNDS = 2
ASSUME_REPLY = "按你的合理假设处理，直接给出方案"
_TEXT_CAP = 4000


class BudgetExceeded(RuntimeError):
    pass


class Budget:
    """A thread-safe cap on model calls (copilot turns and judge calls together)."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self._lock = threading.Lock()

    def take(self) -> None:
        with self._lock:
            if self.used >= self.limit:
                raise BudgetExceeded(f"model call budget of {self.limit} reached")
            self.used += 1


def applicable(
    cases: list[dict[str, Any]], switches: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cases whose ``requires`` hold on this platform, and the ones skipped (reported, never scored)."""
    kept, skipped = [], []
    for case in cases:
        needed = case.get("requires") or {}
        if all(switches.get(key) == value for key, value in needed.items()):
            kept.append(case)
        else:
            skipped.append({"id": case["id"], "requires": needed})
    return kept, skipped


def fill_model(value: Any, model_id: int) -> Any:
    """Put the platform's workflow default model where a fixture says ``__DEFAULT_MODEL__``."""
    if value == "__DEFAULT_MODEL__":
        return model_id
    if isinstance(value, dict):
        return {k: fill_model(v, model_id) for k, v in value.items()}
    if isinstance(value, list):
        return [fill_model(v, model_id) for v in value]
    return value


def answer_for(case: dict[str, Any], rounds: list[checks.Round]) -> str:
    """Design §6.3: answer the topics asked first, then the rest; a case with nothing to answer assumes."""
    answers = case["answers"]
    topical = [t for t in answers if t != "_extra"]
    if not topical:
        return f"{answers['_extra']}；其余{ASSUME_REPLY}" if "_extra" in answers else ASSUME_REPLY
    hit = checks.topics_hit(case["ask"], rounds) if case["ask"] else set()
    ordered = [answers[t] for t in topical if t in hit] + [answers[t] for t in topical if t not in hit]
    return "；".join(ordered + ([answers["_extra"]] if "_extra" in answers else []))


def _fallback_declaration(canvas: dict[str, Any]) -> dict[str, Any]:
    """Node types (and tool names) straight from the canvas, when the decompiler cannot read it."""
    nodes = []
    for node in canvas.get("nodes") or []:
        data = node.get("data") or {}
        nodes.append(
            {
                "id": node.get("id"),
                "type": data.get("type"),
                **({"tool": data.get("name")} if data.get("type") == "tool" else {}),
            }
        )
    return {"nodes": nodes, "edges": canvas.get("edges") or []}


def _declaration(
    canvas: dict[str, Any], templates: list[dict[str, Any]], tools: list[dict[str, Any]], name: str
) -> tuple[dict[str, Any], str]:
    try:
        declared, _ = decompile(canvas, templates, tools, name=name)
        return declared, dump(declared)
    except Exception:  # the evaluation must still score structure and the rest
        declared = _fallback_declaration(canvas)
        return declared, json.dumps(declared, ensure_ascii=False, indent=1)


def run_case(
    case: dict[str, Any],
    api: BishengApi,
    judge: Any,
    *,
    run_id: str,
    index: int,
    budget: Budget,
    templates: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    default_model: int | None = None,
    secrets: tuple[str, ...] = (),
) -> dict[str, Any]:
    """One run of one case: ask / answer / propose, score, judge; the flow it made is always deleted."""
    hidden = (*secrets, *api.secrets)
    record: dict[str, Any] = {
        "id": case["id"],
        "group": case["group"],
        "index": index,
        "rounds": [],
        "asked": False,
        "asked_any": False,
        "card": False,
        "mixed": False,
        "structure_ok": False,
        "must_ok": False,
        "must_failed": [],
        "declaration": None,
        "walkthrough": [],
        "cannot_do": [],
        "assume": [],
        "judgements": [],
        "error": None,
    }
    flow_id = None
    try:
        flow_id = api.create_flow(f"eval-{run_id}-{case['id']}-{index}")
        if case["canvas"] == "empty":
            canvas: dict[str, Any] = {"nodes": [], "edges": []}
        else:
            spec = fill_model(
                load_fixture(case["canvas"]), default_model if default_model is not None else api.default_model()
            )
            canvas = compile_spec(spec, templates if templates is not None else api.templates(), tools or [])
        history: list[dict[str, str]] = []
        message = case["request"]
        rounds: list[checks.Round] = []
        turns: list[Turn] = []
        for round_no in range(1 + MAX_CLARIFY_ROUNDS):
            budget.take()
            turn = api.copilot(flow_id, {"nodes": canvas["nodes"], "edges": canvas["edges"]}, history, message)
            turns.append(turn)
            rounds.append(
                checks.Round(questions=turn.question_lines(), text=turn.text, proposal=turn.proposal is not None)
            )
            record["rounds"].append(
                {
                    "message": message,
                    "text": checks.scrub(turn.text[:_TEXT_CAP], hidden),
                    "questions": turn.question_lines(),
                    "proposal": turn.proposal is not None,
                    "issues": turn.issues,
                    "error": checks.scrub(turn.error, hidden) if turn.error else None,
                    "reasoning_chars": turn.reasoning_chars,
                }
            )
            if turn.proposal is not None or not checks.asked_any(rounds[-1]) or round_no == MAX_CLARIFY_ROUNDS:
                break
            history += [{"role": "user", "content": message}, {"role": "assistant", "content": turn.history_text()}]
            message = answer_for(case, rounds)

        record["asked"] = checks.asked(case["ask"], rounds) if case["ask"] else False
        record["asked_any"] = checks.asked_any(rounds[0])
        record["card"] = checks.asked_by_card(rounds[0])
        record["mixed"] = checks.asked_and_proposed(rounds)
        final = turns[-1]
        replies = "\n".join(part for t in turns for part in [t.text, *(t.question_lines() or [])] if part)
        declaration_text = "（没有给出方案）"
        if final.proposal is not None:
            record["structure_ok"] = not final.issues and not final.error
            declared, declaration_text = _declaration(final.proposal, templates or [], tools or [], case["id"])
            record["declaration"] = declaration_text[:20000]
            failed = [check for check in case["must"] if not checks.must_holds(check, declared)]
            record["must_failed"] = failed
            record["must_ok"] = not failed

        def judged(kind: str, item: str, verdict_reason: tuple[str, str]) -> str:
            verdict, reason = verdict_reason
            record["judgements"].append(
                {"kind": kind, "item": item, "verdict": verdict, "reason": checks.scrub(reason, hidden)}
            )
            return verdict

        for sample in case["walkthrough"]:
            budget.take()
            record["walkthrough"].append(
                judged(
                    "walkthrough",
                    sample["input"],
                    judge.walkthrough(final.text, declaration_text, sample["input"], sample["expect"]),
                )
            )
        for item in case["cannot_do"]:
            budget.take()
            record["cannot_do"].append(
                judged(
                    "cannot_do",
                    item["capability"],
                    judge.cannot_do(replies, item["capability"], item.get("alternative_hint") or []),
                )
            )
        for point in case["assume"]:
            budget.take()
            record["assume"].append(judged("assume", point, judge.assume(final.text, point)))
    except Exception as exc:  # one failing run must not stop the others
        record["error"] = checks.scrub(f"{type(exc).__name__}: {exc}", hidden)
    finally:
        if flow_id is not None:
            try:
                api.delete_flow(flow_id)
            except Exception as exc:
                record["cleanup_error"] = checks.scrub(f"{type(exc).__name__}: {exc}", hidden)
    return record


# ---- output ----------------------------------------------------------------------------------------


def _mark(value: Any) -> str:
    return "✓" if value else "✗"


def _pct(entry: dict[str, Any]) -> str:
    low, high = entry["ci95"]
    return f"{entry['rate'] * 100:.0f}%（{entry['k']}/{entry['n']}，95% 区间 {low * 100:.0f}–{high * 100:.0f}%）"


def write_results(runs: list[dict[str, Any]], out_dir: Path, *, label: str, meta: dict[str, Any]) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    summary = checks.summarize(runs)
    json_path = out_dir / f"{stamp}-{label}.json"
    json_path.write_text(
        json.dumps({"meta": meta, "summary": summary, "runs": runs}, ensure_ascii=False, indent=1), encoding="utf-8"
    )

    names = {
        "hit_rate": "该问时问了（命中率）",
        "false_ask_rate": "不该问时问了（误问率）",
        "card_rate": "以卡片提问",
        "stated_rate": "明确说明做不到",
        "structure": "方案通过结构校验",
        "must": "硬性要求满足",
        "walkthrough": "样例走查正确",
        "cannot_do": "做不到判定（逐项）",
        "assume": "假设写明",
    }
    lines = [
        f"# copilot 评测：{label}",
        "",
        f"- 时间：{stamp}",
        *(f"- {k}：{v}" for k, v in meta.items() if k != "calibration"),
        "",
    ]
    if not meta.get("judge_calibrated", True):
        lines += ["> **评判不可靠**：校准项未全部判对，语义类指标仅供参考。", ""]
    lines += ["## 汇总", "", "| 组 | 指标 | 结果 |", "|---|---|---|"]
    for group, entry in summary.items():
        for key, value in entry.items():
            if isinstance(value, dict) and "rate" in value:
                lines.append(f"| {group} | {names.get(key, key)} | {_pct(value)} |")
        if "mixed" in entry:
            lines.append(f"| {group} | 问了又出方案（不计命中） | {entry['mixed']} 次 |")
    lines += [
        "",
        "## 逐条",
        "",
        "| 用例 | 组 | 次 | 问了 | 结构 | 硬性 | 走查 | 做不到 | 错误 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for run in sorted(runs, key=lambda r: (r["group"], r["id"], r.get("index", 0))):
        lines.append(
            f"| {run['id']} | {run['group']} | {run.get('index', 0)} | {_mark(run.get('asked') or run.get('asked_any'))} | {_mark(run.get('structure_ok'))} "
            f"| {_mark(run.get('must_ok'))} | {'/'.join(run.get('walkthrough') or []) or '—'} | {'/'.join(run.get('cannot_do') or []) or '—'} "
            f"| {(run.get('error') or '')[:80]} |"
        )
    md_path = out_dir / f"{stamp}-{label}.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"json": json_path, "md": md_path}


# ---- entry point -----------------------------------------------------------------------------------


def _env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"missing environment variable {name}")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="F008 copilot evaluation (calls a real platform and model)")
    parser.add_argument("--label", required=True)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--only", default="")
    parser.add_argument("--max-calls", type=int, default=400)
    parser.add_argument("--out", default=str(RESULTS_DIR))
    args = parser.parse_args(argv)

    base, token = _env("BISHENG_EVAL_BASE"), _env("BISHENG_EVAL_TOKEN")
    judge_base, judge_key, judge_model = (
        _env("BISHENG_EVAL_JUDGE_BASE"),
        _env("BISHENG_EVAL_JUDGE_KEY"),
        _env("BISHENG_EVAL_JUDGE_MODEL"),
    )
    hidden = (token, judge_key)
    cases = load_cases([c for c in args.only.split(",") if c] or None)
    run_id = _secrets.token_hex(3)
    budget = Budget(args.max_calls)

    with (
        httpx.Client(base_url=base, trust_env=False, timeout=60) as platform_client,
        httpx.Client(
            base_url=judge_base.rstrip("/"), trust_env=False, headers={"Authorization": f"Bearer {judge_key}"}
        ) as judge_client,
    ):
        api = BishengApi(platform_client, token=token, tenant=os.environ.get("BISHENG_EVAL_TENANT") or None)
        judge = Judge(judge_client, model=judge_model)
        templates, tools, model_id = api.templates(), api.tools(), api.default_model()
        switches = api.platform_switches()
        cases, skipped = applicable(cases, switches)
        for _ in CALIBRATION:
            budget.take()
        calibration = [{k: checks.scrub(str(v), hidden) for k, v in item.items()} for item in judge.calibrate()]
        calibrated = all(item["verdict"] == item["wanted"] for item in calibration)
        started = time.monotonic()
        jobs = [(case, i) for case in cases for i in range(args.repeat)]
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
            runs = list(
                pool.map(
                    lambda job: run_case(
                        job[0],
                        api,
                        judge,
                        run_id=run_id,
                        index=job[1],
                        budget=budget,
                        templates=templates,
                        tools=tools,
                        default_model=model_id,
                        secrets=hidden,
                    ),
                    jobs,
                )
            )
    meta = {
        "run_id": run_id,
        "label": args.label,
        "cases": len(cases),
        "repeat": args.repeat,
        "runs": len(runs),
        "default_model_id": model_id,
        "judge_model": judge_model,
        "platform": switches,
        "skipped": skipped,
        "judge_calibrated": calibrated,
        "model_calls": budget.used,
        "elapsed_s": round(time.monotonic() - started),
        "calibration": calibration,
    }
    paths = write_results(runs, Path(args.out), label=args.label, meta=meta)
    print(f"results: {paths['md']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
