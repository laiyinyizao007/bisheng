import type { CopilotProposal } from "@/controllers/API/workflowCopilot";
import { create } from "zustand";
import type { Attempt } from "./CopilotProgress";

/**
 * The copilot conversation of the workflow being edited (AC-21). It lives outside the
 * editor's components because the editor re-mounts them on Save (the header's
 * ``forceUpdateFlow``): the drawer's open state, its turns and a turn still streaming
 * carry over. It ends, aborting a running turn, only when the editor page is left
 * (``endCopilotSession``) or another workflow is opened (``bindCopilotSession``).
 */

export type Outcome = "applied" | "discarded" | "stopped";

export interface Turn {
    id: number;
    role: "user" | "assistant";
    /** The final reply (``message`` event); while streaming, the attempts hold the text. */
    text: string;
    /** One per model attempt: its streamed explanation and YAML draft. */
    attempts?: Attempt[];
    running?: boolean;
    proposal?: CopilotProposal;
    issues?: string[];
    error?: string;
    outcome?: Outcome;
    /** Digest of the canvas this turn was sent with (AC-19). */
    baseDigest?: string;
}

interface SessionState {
    flowId: string;
    open: boolean;
    turns: Turn[];
    busy: boolean;
    /** Bumped by "new conversation": the message list's scroll session. */
    session: number;
    /** The running turn's request; aborting it is "stop". */
    controller: AbortController | null;
}

const blank = (flowId: string): SessionState => ({ flowId, open: false, turns: [], busy: false, session: 0, controller: null });

export const useCopilotSession = create<SessionState>()(() => blank(""));

// Turn ids never repeat, not even across sessions: a turn aborted by ending its session
// may still settle afterwards, and must not land on a turn of the next one.
let lastId = 0;
export const nextTurnId = (): number => ++lastId;

export function setCopilotOpen(open: boolean): void {
    useCopilotSession.setState({ open });
}

/** The editor shows ``flowId``: a different workflow than the session's starts afresh. */
export function bindCopilotSession(flowId: string): void {
    if (useCopilotSession.getState().flowId !== flowId) endCopilotSession(flowId);
}

/** Leaving the editor: the model call is not left running, and the conversation is gone. */
export function endCopilotSession(flowId = ""): void {
    useCopilotSession.getState().controller?.abort();
    useCopilotSession.setState(blank(flowId));
}

export function updateTurns(change: (turns: Turn[]) => Turn[]): void {
    useCopilotSession.setState((state) => ({ turns: change(state.turns) }));
}

export function updateTurn(id: number, patch: Partial<Turn> | ((turn: Turn) => Partial<Turn>)): void {
    updateTurns((all) => all.map((turn) => (turn.id === id ? { ...turn, ...(typeof patch === "function" ? patch(turn) : patch) } : turn)));
}

/** A turn started: it owns the session until ``finishTurn`` with the same controller. */
export function startTurn(controller: AbortController): void {
    useCopilotSession.setState({ controller, busy: true });
}

/** A turn settled; one from an ended session (another controller now, or none) changes nothing. */
export function finishTurn(controller: AbortController): void {
    if (useCopilotSession.getState().controller === controller) useCopilotSession.setState({ controller: null, busy: false });
}

export function restartConversation(): void {
    useCopilotSession.setState((state) => ({ turns: [], session: state.session + 1 }));
}
