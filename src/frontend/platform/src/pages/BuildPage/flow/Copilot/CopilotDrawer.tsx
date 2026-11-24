import { bsConfirm } from "@/components/bs-ui/alertDialog/useConfirm";
import {
    checkCopilotAvailability,
    streamCopilot,
    type CopilotEvent,
    type CopilotProposal,
    type CopilotTurn,
} from "@/controllers/API/workflowCopilot";
import { ConversationComposer } from "@bisheng/chat";
import { Button } from "@bisheng/ui";
import type { Edge, Node, Viewport } from "@xyflow/react";
import { Outlined } from "bisheng-icons";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { canvasDigest } from "./canvasDigest";
import DiffPreview, { label } from "./DiffPreview";

/**
 * F007 copilot drawer (design decisions 7, 9, 10). Kept mounted while the editor is open
 * and only hidden when closed, so the conversation survives closing the drawer and is
 * gone when the page is left (AC-21). Nothing here saves: "apply" hands the merged canvas
 * to the editor, which snapshots it for undo (AC-18).
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

type Outcome = "applied" | "discarded" | "stopped";

interface Turn {
    id: number;
    role: "user" | "assistant";
    text: string;
    status?: string;
    proposal?: CopilotProposal;
    issues?: string[];
    error?: string;
    outcome?: Outcome;
    /** Digest of the canvas this turn was sent with (AC-19). */
    baseDigest?: string;
}

type Availability = { state: "checking" } | { state: "ok" } | { state: "unavailable"; reason: string };
type Translate = (key: string, options?: Record<string, unknown>) => string;

export default function CopilotDrawer({ open, flowId, getCanvas, onApply, onClose }: Props) {
    const { t } = useTranslation("flow");
    const tr = t as unknown as Translate;
    const inputRef = useRef<HTMLTextAreaElement>(null);
    const controllerRef = useRef<AbortController | null>(null);
    const nextId = useRef(1);
    const [turns, setTurns] = useState<Turn[]>([]);
    const [busy, setBusy] = useState(false);
    const [availability, setAvailability] = useState<Availability>({ state: "checking" });

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

    const update = useCallback((id: number, patch: Partial<Turn> | ((turn: Turn) => Partial<Turn>)) => {
        setTurns((all) => all.map((turn) => (turn.id === id ? { ...turn, ...(typeof patch === "function" ? patch(turn) : patch) } : turn)));
    }, []);

    const send = useCallback(async () => {
        const message = inputRef.current?.value.trim() ?? "";
        if (!message || busy) return;
        if (inputRef.current) inputRef.current.value = "";

        const canvas = getCanvas();
        const history: CopilotTurn[] = turns.filter((turn) => turn.text).map((turn) => ({ role: turn.role, content: turn.text }));
        const userTurn: Turn = { id: nextId.current++, role: "user", text: message };
        const reply: Turn = { id: nextId.current++, role: "assistant", text: "", baseDigest: canvasDigest(canvas.nodes, canvas.edges) };
        setTurns((all) => [...all, userTurn, reply]);

        const controller = new AbortController();
        controllerRef.current = controller;
        setBusy(true);
        try {
            const request = { flow_id: flowId, canvas: { nodes: canvas.nodes, edges: canvas.edges, viewport: canvas.viewport }, history, message };
            for await (const event of streamCopilot(request, controller.signal)) {
                applyEvent(reply.id, event, update, tr);
            }
        } catch (error) {
            if (controller.signal.aborted) {
                update(reply.id, { outcome: "stopped", status: undefined });
            } else {
                const reason = error instanceof Error && error.message ? error.message : String(error);
                update(reply.id, { error: reason, status: undefined });
            }
        } finally {
            controllerRef.current = null;
            setBusy(false);
            update(reply.id, { status: undefined });
        }
    }, [busy, flowId, getCanvas, tr, turns, update]);

    const stop = useCallback(() => {
        controllerRef.current?.abort();
    }, []);

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
            className={`${open ? "" : "hidden"} fixed right-0 bottom-0 z-10 flex h-full w-[440px] flex-col border bg-[#fff] shadow-sm dark:bg-[#1B1B1B]`}
            data-testid="copilot-drawer"
            onKeyDown={(event) => event.stopPropagation()}
        >
            <div className="flex items-center justify-between px-4 py-2">
                <span className="text-sm font-bold">{label(tr, "copilot.title", "Workflow Copilot")}</span>
                <Button size="medium" variant="text" color="default" iconOnly aria-label={label(tr, "copilot.close", "Close")} onClick={onClose}>
                    <Outlined.Close size={16} />
                </Button>
            </div>
            <div className="flex-1 space-y-3 overflow-y-auto px-4 py-2 text-sm">
                {availability.state === "checking" && <div className="text-gray-500">{label(tr, "copilot.checking", "Checking…")}</div>}
                {availability.state === "unavailable" && (
                    <div className="rounded-md bg-red-50 p-2 text-red-700 dark:bg-red-950 dark:text-red-300" data-testid="copilot-unavailable">
                        {label(tr, "copilot.unavailable", availability.reason, { reason: availability.reason })}
                    </div>
                )}
                {!turns.length && canExecute && <div className="text-gray-500">{label(tr, "copilot.empty", "")}</div>}
                {turns.map((turn) => (
                    <TurnView key={turn.id} turn={turn} busy={busy} t={tr} onApply={() => apply(turn)} onDiscard={() => update(turn.id, { outcome: "discarded" })} />
                ))}
            </div>
            <ConversationComposer
                inputRef={inputRef}
                canExecute={canExecute}
                busy={busy}
                showStop={busy}
                onStop={stop}
                showRestart={!busy && turns.length > 0}
                onRestart={() => setTurns([])}
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

function applyEvent(id: number, event: CopilotEvent, update: (id: number, patch: Partial<Turn> | ((turn: Turn) => Partial<Turn>)) => void, t: Translate) {
    switch (event.type) {
        case "status":
            update(id, {
                // A retry streams a fresh answer: the rejected attempt's text is not kept.
                ...(event.stage === "retrying" ? { text: "" } : {}),
                status:
                    event.stage === "retrying"
                        ? label(t, "copilot.statusRetrying", `#${event.attempt}`, { attempt: event.attempt })
                        : event.stage === "validating"
                          ? label(t, "copilot.statusValidating", "Validating…")
                          : label(t, "copilot.statusGenerating", "Generating…"),
            });
            break;
        case "delta":
            update(id, (turn) => ({ text: turn.text + event.text }));
            break;
        case "message":
            update(id, { text: event.text });
            break;
        case "proposal":
            update(id, {
                proposal: { canvas: event.canvas, diff: event.diff, needs_selection: event.needs_selection, replaced: event.replaced },
            });
            break;
        case "issues":
            update(id, { issues: event.issues });
            break;
        case "error":
            update(id, { error: event.message });
            break;
        case "done":
            break;
    }
}

function TurnView({ turn, busy, t, onApply, onDiscard }: { turn: Turn; busy: boolean; t: Translate; onApply: () => void; onDiscard: () => void }) {
    if (turn.role === "user") {
        return (
            <div className="ml-8 whitespace-pre-wrap rounded-lg bg-gray-100 p-2 dark:bg-gray-800" data-testid="copilot-user">
                {turn.text}
            </div>
        );
    }
    const pending = turn.proposal && !turn.outcome;
    return (
        <div className="mr-4" data-testid="copilot-assistant">
            {turn.text && <div className="whitespace-pre-wrap">{turn.text}</div>}
            {turn.status && <div className="text-gray-500">{turn.status}</div>}
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
            {pending && (
                <div className="mt-2 flex gap-2">
                    <Button size="medium" variant="solid" disabled={busy} onClick={onApply}>
                        {label(t, "copilot.apply", "Apply")}
                    </Button>
                    <Button size="medium" variant="outlined" color="default" onClick={onDiscard}>
                        {label(t, "copilot.discard", "Discard")}
                    </Button>
                </div>
            )}
            {turn.outcome && (
                <div className="mt-1 text-gray-500" data-testid="copilot-outcome">
                    {turn.outcome === "applied"
                        ? label(t, "copilot.applied", "Applied")
                        : turn.outcome === "discarded"
                          ? label(t, "copilot.discarded", "Discarded")
                          : label(t, "copilot.stopped", "Stopped")}
                </div>
            )}
        </div>
    );
}
