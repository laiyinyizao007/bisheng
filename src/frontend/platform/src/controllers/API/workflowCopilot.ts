import axios from "@/controllers/request";
import { requestStream } from "@/controllers/requestStream";

/** F007 workflow copilot: the editor's side of `POST /api/v1/workflow/copilot`. */

export interface CopilotTurn {
    role: "user" | "assistant";
    content: string;
}

export interface CopilotCanvas {
    nodes: unknown[];
    edges: unknown[];
    viewport?: unknown;
}

export interface CopilotNodeSummary {
    id: string;
    type?: string | null;
    name?: string | null;
}

export interface CopilotParamChange {
    key: string;
    online?: unknown;
    local?: unknown;
    only?: "online" | "local";
}

export interface CopilotNodeChange {
    id: string;
    fields: { field: string; online?: unknown; local?: unknown }[];
    params: CopilotParamChange[];
}

export interface CopilotEdgeSummary {
    source: string;
    sourceHandle?: string | null;
    target: string;
}

/** `diff(local = proposal, online = the canvas as sent)`: `*_only_local` were added, `*_only_online` removed. */
export interface CopilotDiff {
    identical: boolean;
    nodes_only_local: CopilotNodeSummary[];
    nodes_only_online: CopilotNodeSummary[];
    nodes_changed: CopilotNodeChange[];
    edges_only_local: CopilotEdgeSummary[];
    edges_only_online: CopilotEdgeSummary[];
}

export interface CopilotNeed {
    node_id: string;
    param_key: string;
    /** `tool_unchanged`: an existing tool node kept as it was because the new tool was not found. */
    kind: "model" | "knowledge" | "tool" | "tool_unchanged";
    wanted: string;
    candidates: string[];
}

export interface CopilotProposal {
    canvas: { nodes: unknown[]; edges: unknown[]; viewport?: unknown };
    diff: CopilotDiff;
    needs_selection: CopilotNeed[];
    replaced: string[];
}

export type CopilotEvent =
    | { type: "status"; stage: "generating" | "retrying" | "validating"; attempt: number }
    /** A reasoning model's thinking as it streams: shown only, never part of the reply. */
    | { type: "reasoning"; text: string }
    | { type: "delta"; text: string }
    /** The proposal's YAML as the model writes it: progress only, checked later. */
    | { type: "draft"; text: string }
    | { type: "message"; text: string }
    | ({ type: "proposal" } & CopilotProposal)
    | { type: "issues"; issues: string[] }
    | { type: "error"; code: number; message: string }
    | { type: "done" };

export interface CopilotRequest {
    flow_id: string;
    canvas: CopilotCanvas;
    history: CopilotTurn[];
    message: string;
}

const COPILOT_URL = "/api/v1/workflow/copilot";

/**
 * Resolves when the copilot can run for this workflow; rejects with the request
 * layer's error (localized `api_errors` copy for 10560 / 10561 / no permission).
 * Silent: the drawer shows the reason itself (AC-03).
 */
export async function checkCopilotAvailability(flowId: string): Promise<void> {
    await axios.get(`${COPILOT_URL}/availability`, { params: { flow_id: flowId }, silent: true } as never);
}

/** One copilot turn as events; stops early (and cancels the server side) when `signal` aborts. */
export async function* streamCopilot(request: CopilotRequest, signal?: AbortSignal): AsyncGenerator<CopilotEvent> {
    const body = await requestStream(COPILOT_URL, signal, request);
    yield* readNdjson(body);
}

/** NDJSON → parsed objects, one per line, whatever the chunk boundaries. */
export async function* readNdjson(body: ReadableStream<Uint8Array>): AsyncGenerator<CopilotEvent> {
    const reader = body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buffer = "";
    try {
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let newline = buffer.indexOf("\n");
            while (newline >= 0) {
                const line = buffer.slice(0, newline).trim();
                buffer = buffer.slice(newline + 1);
                if (line) yield JSON.parse(line) as CopilotEvent;
                newline = buffer.indexOf("\n");
            }
        }
        const rest = (buffer + decoder.decode()).trim();
        if (rest) yield JSON.parse(rest) as CopilotEvent;
    } finally {
        reader.releaseLock();
    }
}
