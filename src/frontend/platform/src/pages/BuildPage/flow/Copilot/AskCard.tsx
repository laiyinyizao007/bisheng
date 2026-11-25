import { Button } from "@bisheng/ui";
import { useState } from "react";
import type { CopilotQuestion } from "@/controllers/API/workflowCopilot";
import { label } from "./DiffPreview";

/**
 * F008: the copilot asks before it proposes (design §5). Each question is a row of options
 * plus "Other" (a text box that must be filled); "Continue with these" is enabled once every
 * question has an answer. After it is submitted — or the user sends something else instead —
 * the card is locked and only shows what was chosen.
 */

export type AskState = "open" | "answered" | "skipped";

type Translate = (key: string, options?: Record<string, unknown>) => string;

/** What a question's pick holds: an option's index, or "other". */
type Pick = number | "other";

interface Props {
    questions: CopilotQuestion[];
    state: AskState;
    answers?: Record<string, string>;
    busy: boolean;
    t: Translate;
    onSubmit: (answers: Record<string, string>) => void;
}

/** The next user message the card sends: "question：answer；question：answer". */
export function answersMessage(questions: CopilotQuestion[], answers: Record<string, string>): string {
    return questions.map((q) => `${q.question}：${answers[q.id] ?? ""}`).join("；");
}

export default function AskCard({ questions, state, answers, busy, t, onSubmit }: Props) {
    const [picked, setPicked] = useState<Record<string, Pick>>({});
    const [other, setOther] = useState<Record<string, string>>({});

    if (state !== "open") {
        return (
            <div className="mt-2 rounded-md border px-3 py-2 text-sm" data-testid="copilot-ask-locked">
                {state === "skipped" && <div className="mb-1 text-gray-500">{label(t, "copilot.askSkipped", "Not answered")}</div>}
                {questions.map((q) => (
                    <div key={q.id} className="mb-1">
                        <span className="text-gray-500">{q.question}</span>
                        {state === "answered" && answers?.[q.id] && <span className="ml-2">✓ {answers[q.id]}</span>}
                    </div>
                ))}
            </div>
        );
    }

    const resolved: Record<string, string> = {};
    for (const q of questions) {
        const choice = picked[q.id];
        const text = choice === "other" ? (other[q.id] ?? "").trim() : choice === undefined ? "" : q.options[choice];
        if (text) resolved[q.id] = text;
    }
    const complete = questions.every((q) => resolved[q.id]);

    const option = (q: CopilotQuestion, value: Pick, text: string) => {
        const active = picked[q.id] === value;
        return (
            <button
                key={value}
                type="button"
                aria-pressed={active}
                disabled={busy}
                onClick={() => setPicked((all) => ({ ...all, [q.id]: value }))}
                className={`rounded-md border px-2 py-1 text-xs ${active ? "border-primary bg-primary/10 text-primary" : "hover:border-primary/50"}`}
            >
                {text}
            </button>
        );
    };

    return (
        <div className="mt-2 space-y-3 rounded-md border px-3 py-2 text-sm" data-testid="copilot-ask">
            {questions.map((q) => (
                <div key={q.id}>
                    <div className="mb-1 font-medium">{q.question}</div>
                    <div className="flex flex-wrap gap-2">
                        {q.options.map((text, index) => option(q, index, text))}
                        {option(q, "other", label(t, "copilot.askOther", "Other"))}
                    </div>
                    {picked[q.id] === "other" && (
                        <input
                            className="mt-2 w-full rounded-md border px-2 py-1 text-xs"
                            value={other[q.id] ?? ""}
                            placeholder={label(t, "copilot.askOtherPlaceholder", "Type your answer")}
                            onChange={(event) => setOther((all) => ({ ...all, [q.id]: event.target.value }))}
                        />
                    )}
                </div>
            ))}
            <Button size="medium" variant="solid" disabled={busy || !complete} onClick={() => onSubmit(resolved)}>
                {label(t, "copilot.askContinue", "Continue with these")}
            </Button>
        </div>
    );
}
