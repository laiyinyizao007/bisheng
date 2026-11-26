import { bsConfirm } from "@/components/bs-ui/alertDialog/useConfirm";
import {
    checkCopilotAvailability,
    streamCopilot,
    type CopilotEvent,
    type CopilotTurn,
} from "@/controllers/API/workflowCopilot";
import { PlatformChatMarkdown } from "@/components/bs-comp/appChat/PlatformChatMarkdown";
import { ConversationComposer, MessageBubble, useConversationScroll } from "@bisheng/chat";
import { Button } from "@bisheng/ui";
import type { Edge, Node, Viewport } from "@xyflow/react";
import { Outlined } from "bisheng-icons";
import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { canvasDigest } from "./canvasDigest";
import CopilotProgress, { type Attempt, type ProgressEnd } from "./CopilotProgress";
import DiffPreview, { label } from "./DiffPreview";
import {
    bindCopilotSession,
    finishTurn,
    nextTurnId,
    restartConversation,
    startTurn,
    updateTurn,
    updateTurns,
    useCopilotSession,
    type Turn,
} from "./session";

/**
 * F007 copilot drawer (design decisions 7, 9, 10). The conversation lives in the copilot
 * session (``./session``), not here: the editor re-mounts this component on Save, and the
 * conversation, a turn still streaming included, carries over; it is gone when the page is
 * left (AC-21). Nothing here saves: "apply" hands the merged canvas to the editor, which
 * snapshots it for undo (AC-18).
 */

export interface CanvasState {
    nodes: Node[];
    edges: Edge[];
    viewport?: Viewport;
}

interface Props {
    open: boolean;
    flowId: string;
    getCanvas: () => CanvasState;
    onApply: (canvas: { nodes: Node[]; edges: Edge[] }) => void;
    onClose: () => void;
}

type Availability = { state: "checking" } | { state: "ok" } | { state: "unavailable"; reason: string };
type Translate = (key: string, options?: Record<string, unknown>) => string;

export default function CopilotDrawer({ open, flowId, getCanvas, onApply, onClose }: Props) {
    const { t } = useTranslation("flow");
    const tr = t as unknown as Translate;
    const inputRef = useRef<HTMLTextAreaElement>(null);
    const scrollRef = useRef<HTMLDivElement>(null);
    const [availability, setAvailability] = useState<Availability>({ state: "checking" });
    useEffect(() => bindCopilotSession(flowId), [flowId]);
    // Until the session is bound to this workflow, it may still hold another one's turns.
    const bound = useCopilotSession((state) => state.flowId === flowId);
    const storedTurns = useCopilotSession((state) => state.turns);
    const turns = bound ? storedTurns : NO_TURNS;
    const busy = useCopilotSession((state) => bound && state.busy);
    const session = useCopilotSession((state) => state.session);

    // The chat's follow-the-bottom policy: every streamed batch re-renders the list, and
    // scrolling up to read stops the following until the next message is sent. The hook
    // resumes following when the newest entry is the user's; the copilot adds its reply in
    // the same step as the question, so a reply still running counts as the user's.
    const scrollMessages = useMemo(
        () => turns.map((turn) => ({ key: `${turn.id}`, kind: turn.role === "user" || turn.running ? "user" : "answer" })),
        [turns],
    );
    useConversationScroll({ scrollRef, sessionKey: `copilot-${session}`, messages: scrollMessages, canLoadOlder: false, onLoadOlder: () => undefined });

    useEffect(() => {
        if (!open || !flowId) return;
        let live = true;
        setAvailability({ state: "checking" });
        checkCopilotAvailability(flowId).then(
            () => live && setAvailability({ state: "ok" }),
            (error: unknown) =>
                live && setAvailability({ state: "unavailable", reason: error instanceof Error && error.message ? error.message : String(error) }),
        );
        return () => {
            live = false;
        };
    }, [open, flowId]);

    const update = updateTurn;

    const send = useCallback(async () => {
        const message = inputRef.current?.value.trim() ?? "";
        if (!message || busy) return;
        if (inputRef.current) inputRef.current.value = "";

        const canvas = getCanvas();
        // Only final replies: a stopped turn's half-streamed text is not the model's answer.
        const history: CopilotTurn[] = turns.filter((turn) => turn.text).map((turn) => ({ role: turn.role, content: turn.text }));
        const userTurn: Turn = { id: nextTurnId(), role: "user", text: message };
        const reply: Turn = { id: nextTurnId(), role: "assistant", text: "", attempts: [], running: true, baseDigest: canvasDigest(canvas.nodes, canvas.edges) };
        updateTurns((all) => [...all, userTurn, reply]);

        const controller = new AbortController();
        startTurn(controller);
        // Events are applied in batches (one render per FLUSH_MS), not one render per chunk:
        // a long draft arrives in hundreds of pieces.
        const queued: CopilotEvent[] = [];
        let timer: ReturnType<typeof setTimeout> | undefined;
        const flush = () => {
            clearTimeout(timer);
            timer = undefined;
            const batch = queued.splice(0);
            if (batch.length) update(reply.id, (turn) => batch.reduce((acc, event) => ({ ...acc, ...reduceEvent(acc, event, tr) }), turn));
        };
        try {
            const request = { flow_id: flowId, canvas: { nodes: canvas.nodes, edges: canvas.edges, viewport: canvas.viewport }, history, message };
            for await (const event of streamCopilot(request, controller.signal)) {
                queued.push(event);
                timer ??= setTimeout(flush, FLUSH_MS);
            }
            flush();
        } catch (error) {
            flush();
            if (controller.signal.aborted) {
                // A proposal that already arrived stays choosable: stop only ends the wait.
                update(reply.id, (turn) => (turn.proposal ? {} : { outcome: "stopped" }));
            } else {
                const reason = error instanceof Error && error.message ? error.message : String(error);
                update(reply.id, { error: reason });
            }
        } finally {
            finishTurn(controller);
            update(reply.id, { running: false });
        }
    }, [busy, flowId, getCanvas, tr, turns, update]);

    const stop = useCallback(() => {
        useCopilotSession.getState().controller?.abort();
    }, []);

    const discard = useCallback((turn: Turn) => update(turn.id, { outcome: "discarded" }), [update]);

    const apply = useCallback(
        (turn: Turn) => {
            const proposal = turn.proposal;
            if (!proposal) return;
            const land = () => {
                onApply({ nodes: proposal.canvas.nodes as Node[], edges: proposal.canvas.edges as Edge[] });
                update(turn.id, { outcome: "applied" });
            };
            const current = getCanvas();
            if (turn.baseDigest !== undefined && canvasDigest(current.nodes, current.edges) !== turn.baseDigest) {
                bsConfirm({
                    title: label(tr, "copilot.confirmTitle", "Overwrite canvas edits?"),
                    desc: label(tr, "copilot.confirmDesc", "Edits made after sending will be overwritten."),
                    okTxt: label(tr, "copilot.confirmOk", "Apply anyway"),
                    canelTxt: label(tr, "copilot.confirmCancel", "Cancel"),
                    onOk(next: () => void) {
                        land();
                        next();
                    },
                });
                return;
            }
            land();
        },
        [getCanvas, onApply, tr, update],
    );

    const canExecute = availability.state === "ok";
    return (
        <div
            // A column beside the canvas, not a layer over it: the canvas narrows while it is
            // open, so fitting the view never puts nodes behind it.
            className={`${open ? "" : "hidden"} z-10 flex h-full w-[440px] shrink-0 flex-col border bg-[#fff] shadow-sm dark:bg-[#1B1B1B]`}
            data-testid="copilot-drawer"
            onKeyDown={(event) => event.stopPropagation()}
        >
            <div className="flex items-center justify-between px-4 py-2">
                <span className="text-sm font-bold">{label(tr, "copilot.title", "Workflow Copilot")}</span>
                <Button size="medium" variant="text" color="default" iconOnly aria-label={label(tr, "copilot.close", "Close")} onClick={onClose}>
                    <Outlined.Close size={16} />
                </Button>
            </div>
            {/* pb-14: the composer floats "new conversation" 56px above itself, over this list */}
            <div ref={scrollRef} className="flex-1 space-y-1 overflow-y-auto px-2 pb-14 pt-2 text-sm">
                {availability.state === "checking" && <div className="text-gray-500">{label(tr, "copilot.checking", "Checking…")}</div>}
                {availability.state === "unavailable" && (
                    <div className="rounded-md bg-red-50 p-2 text-red-700 dark:bg-red-950 dark:text-red-300" data-testid="copilot-unavailable">
                        {label(tr, "copilot.unavailable", availability.reason, { reason: availability.reason })}
                    </div>
                )}
                {!turns.length && canExecute && <div className="text-gray-500">{label(tr, "copilot.empty", "")}</div>}
                {turns.map((turn) => (
                    <TurnView key={turn.id} turn={turn} busy={busy} t={tr} onApply={apply} onDiscard={discard} />
                ))}
            </div>
            <ConversationComposer
                inputRef={inputRef}
                canExecute={canExecute}
                busy={busy}
                showStop={busy}
                onStop={stop}
                showRestart={!busy && turns.length > 0}
                onRestart={restartConversation}
                onSend={send}
                labels={{
                    send: label(tr, "copilot.send", "Send"),
                    stop: label(tr, "copilot.stop", "Stop"),
                    restart: label(tr, "copilot.restart", "New conversation"),
                }}
                placeholder={label(tr, "copilot.placeholder", "")}
                hint={label(tr, "copilot.hint", "")}
            />
        </div>
    );
}

const FLUSH_MS = 50;
const NO_TURNS: Turn[] = [];

/** What one streamed event changes in its reply turn. */
function reduceEvent(turn: Turn, event: CopilotEvent, t: Translate): Partial<Turn> {
    const attempts = turn.attempts ?? [];
    switch (event.type) {
        case "status":
            if (event.stage === "validating") return { attempts: withLast(attempts, { stage: "validating" }) };
            // Each (re)try streams a fresh answer into its own attempt; the rejected
            // one's text stays on screen until the new one has something to show.
            return { attempts: [...attempts, { attempt: event.attempt, stage: "generating", text: "", reasoning: "", draft: "" }] };
        case "reasoning":
            return { attempts: withLast(attempts, (last) => ({ reasoning: last.reasoning + event.text })) };
        case "delta":
            return { attempts: withLast(attempts, (last) => ({ text: last.text + event.text })) };
        case "draft":
            return { attempts: withLast(attempts, (last) => ({ draft: last.draft + event.text })) };
        case "message":
            return { text: event.text };
        case "proposal":
            return { proposal: { canvas: event.canvas, diff: event.diff, needs_selection: event.needs_selection, replaced: event.replaced } };
        case "issues":
            return { issues: event.issues };
        case "error": {
            // The code's localized copy (AC-27), keeping the exception class the server names.
            const kind = /（([A-Za-z0-9_]+)）$/.exec(event.message)?.[1];
            const text = label(t, `api_errors:${event.code}`, event.message);
            return { error: kind && text !== event.message ? `${text}（${kind}）` : text };
        }
        case "done":
            return {};
    }
}

/** Patch the last attempt (a stray piece before any status starts attempt 1). */
function withLast(attempts: Attempt[], patch: Partial<Attempt> | ((last: Attempt) => Partial<Attempt>)): Attempt[] {
    const all = attempts.length ? attempts : [{ attempt: 1, stage: "generating" as const, text: "", reasoning: "", draft: "" }];
    const last = all[all.length - 1];
    return [...all.slice(0, -1), { ...last, ...(typeof patch === "function" ? patch(last) : patch) }];
}

/** The reply once it is final; while streaming, the newest attempt that has said something. */
function shownText(turn: Turn): string {
    if (turn.text || turn.role === "user") return turn.text;
    return [...(turn.attempts ?? [])].reverse().find((attempt) => attempt.text)?.text ?? "";
}

function progressEnd(turn: Turn): ProgressEnd {
    if (turn.running) return "running";
    if (turn.outcome === "stopped") return "stopped";
    if (turn.outcome === "discarded") return "discarded";
    if (turn.proposal) return "done";
    if (turn.error) return "error";
    if (turn.issues) return "failed";
    return "answered"; // a text reply: nothing was proposed, nothing failed
}

const USER_AVATAR = typeof __APP_ENV__ !== "undefined" ? `${__APP_ENV__.BASE_URL}/assets/user.png` : "";

interface TurnViewProps {
    turn: Turn;
    busy: boolean;
    t: Translate;
    onApply: (turn: Turn) => void;
    onDiscard: (turn: Turn) => void;
}

/** Memoized: while a reply streams, only that turn re-renders, not the earlier diffs. */
const TurnView = memo(function TurnView({ turn, busy, t, onApply, onDiscard }: TurnViewProps) {
    if (turn.role === "user") {
        return (
            <div data-testid="copilot-user">
                <MessageBubble logo={USER_AVATAR ? <img className="h-6 w-6 min-w-6" src={USER_AVATAR} alt="" /> : undefined}>
                    <div className="whitespace-break-spaces break-all text-sm text-[#0D1638] dark:text-[#CFD5E8]">{turn.text}</div>
                </MessageBubble>
            </div>
        );
    }
    const text = shownText(turn);
    const choosing = turn.proposal && !turn.outcome;
    const progress = turn.attempts?.length ? <CopilotProgress attempts={turn.attempts} end={progressEnd(turn)} t={t} /> : undefined;
    const stopped = turn.outcome === "stopped";
    const hasBody = !!(text || turn.issues || turn.error || turn.proposal || (turn.outcome && !stopped));
    return (
        <div data-testid="copilot-assistant">
            <MessageBubble
                reasoning={progress}
                hideBody={!!progress && !hasBody}
                // The chat's spinner: only until the first status starts the progress box.
                pending={!!turn.running && !progress && !hasBody}
                status={
                    stopped ? (
                        <p role="status" className="ml-4 mt-2 text-body-sm text-text-3" data-testid="copilot-outcome">
                            {label(t, "copilot.stopped", "Stopped")}
                        </p>
                    ) : undefined
                }
            >
                {text && (
                    <div className="bs-mkdown break-words text-sm [word-break:break-all]">
                        <PlatformChatMarkdown content={text} />
                    </div>
                )}
                {turn.issues && (
                    <div className="mt-2 rounded-md bg-red-50 p-2 text-red-700 dark:bg-red-950 dark:text-red-300" data-testid="copilot-issues">
                        <div>{label(t, "copilot.issuesTitle", "The proposal did not pass validation:")}</div>
                        <ul className="ml-4 list-disc">
                            {turn.issues.map((issue, index) => (
                                <li key={index}>{issue}</li>
                            ))}
                        </ul>
                    </div>
                )}
                {turn.error && (
                    <div className="mt-2 text-red-600" data-testid="copilot-error">
                        {label(t, "copilot.failed", turn.error, { message: turn.error })}
                    </div>
                )}
                {turn.proposal && <DiffPreview proposal={turn.proposal} />}
                {choosing && (
                    <div className="mt-2 flex gap-2">
                        <Button size="medium" variant="solid" disabled={busy} onClick={() => onApply(turn)}>
                            {label(t, "copilot.apply", "Apply")}
                        </Button>
                        <Button size="medium" variant="outlined" color="default" onClick={() => onDiscard(turn)}>
                            {label(t, "copilot.discard", "Discard")}
                        </Button>
                    </div>
                )}
                {turn.outcome && !stopped && (
                    <div className="mt-1 text-gray-500" data-testid="copilot-outcome">
                        {turn.outcome === "applied" ? label(t, "copilot.applied", "Applied") : label(t, "copilot.discarded", "Discarded")}
                    </div>
                )}
            </MessageBubble>
        </div>
    );
});
