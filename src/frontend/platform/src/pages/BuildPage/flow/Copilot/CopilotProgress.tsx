import { Outlined } from "bisheng-icons";
import { useEffect, useRef, useState } from "react";
import { label } from "./DiffPreview";

/**
 * The "how far along" box of a copilot answer, laid out like the chat's reasoning box
 * (`ReasoningDetails` in @bisheng/chat): a spinner and the current step while it runs,
 * the proposal's YAML streaming underneath; it folds itself away once the turn ends.
 * The chat's box splits lines into paragraphs, which would lose YAML's indentation, so
 * the layout is reproduced here rather than reused.
 */

export interface Attempt {
    attempt: number;
    stage: "generating" | "validating";
    text: string;
    draft: string;
}

export type ProgressEnd = "running" | "done" | "discarded" | "failed" | "error" | "stopped";

type Translate = (key: string, options?: Record<string, unknown>) => string;

export default function CopilotProgress({ attempts, end, t }: { attempts: Attempt[]; end: ProgressEnd; t: Translate }) {
    const running = end === "running";
    const [chosen, setChosen] = useState<boolean>();
    const open = chosen ?? running;
    const body = useRef<HTMLPreElement>(null);
    const current = attempts[attempts.length - 1];

    // Opening or closing it by hand lasts for the run: when the run ends it folds anyway.
    useEffect(() => {
        if (!running) setChosen(undefined);
    }, [running]);

    useEffect(() => {
        if (running && body.current) body.current.scrollTop = body.current.scrollHeight;
    }, [running, current?.draft]);

    // A plain text answer (no proposal written) needs no progress once it is over.
    if (!current || (!running && !attempts.some((a) => a.draft))) return null;

    const title = running
        ? current.stage === "validating"
            ? label(t, "copilot.statusValidating", "Validating…")
            : current.attempt > 1
              ? label(t, "copilot.statusRetrying", `#${current.attempt}`, { attempt: current.attempt })
              : label(t, "copilot.statusGenerating", "Generating…")
        : end === "done"
          ? label(t, "copilot.progressDone", "Proposal ready")
          : end === "discarded"
            ? label(t, "copilot.discarded", "Discarded")
          : end === "failed"
            ? label(t, "copilot.progressFailed", "The proposal did not validate")
            : end === "error"
              ? label(t, "copilot.progressError", "Generation interrupted")
              : label(t, "copilot.stopped", "Stopped");
    const icon = running ? (
        <Outlined.Loading size={16} className="animate-spin text-primary duration-300" />
    ) : end === "done" ? (
        <Outlined.CheckCircle size={16} className="text-success" />
    ) : end === "stopped" || end === "discarded" ? (
        <Outlined.Square size={16} className="text-text-3" />
    ) : (
        <Outlined.CloseCircle size={16} className="text-danger" />
    );
    const toggle = () => setChosen(!open);

    return (
        <div className="mb-2 py-1" data-testid="copilot-progress">
            <div className="rounded-sm border">
                <div
                    role="button"
                    tabIndex={0}
                    aria-expanded={open}
                    className="flex cursor-pointer items-center justify-between px-3 py-2"
                    onClick={toggle}
                    onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                            event.preventDefault();
                            toggle();
                        }
                    }}
                >
                    <div className="flex items-center gap-2 text-sm font-bold" data-testid="copilot-progress-title">
                        {icon}
                        <span>{title}</span>
                    </div>
                    <Outlined.Down size={16} className={open ? "rotate-180" : undefined} />
                </div>
                {open && (
                    <div className="bg-[#F5F6F8] px-3 py-2 text-xs dark:bg-[#313336]">
                        {attempts.slice(0, -1).map((a) => (
                            <p key={a.attempt} className="mb-1 text-muted-foreground">
                                {label(t, "copilot.progressRejected", `#${a.attempt}`, { attempt: a.attempt })}
                            </p>
                        ))}
                        {current.draft && (
                            <pre ref={body} className="max-h-64 overflow-auto whitespace-pre font-mono text-muted-foreground" data-testid="copilot-draft">
                                {current.draft}
                            </pre>
                        )}
                    </div>
                )}
            </div>
        </div>
    );
}
