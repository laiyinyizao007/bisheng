import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CopilotEvent, CopilotRequest } from "@/controllers/API/workflowCopilot";

// F007 T019: the drawer's own behaviour, with the transport, the confirm dialog and the
// shared composer replaced by small stand-ins.

const mocks = vi.hoisted(() => ({
    check: vi.fn(),
    stream: vi.fn(),
    confirm: vi.fn(),
}));

vi.mock("@/controllers/API/workflowCopilot", () => ({
    checkCopilotAvailability: mocks.check,
    streamCopilot: mocks.stream,
}));
vi.mock("@/components/bs-ui/alertDialog/useConfirm", () => ({ bsConfirm: mocks.confirm }));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => key }) }));
vi.mock("bisheng-icons", () => ({ Outlined: { Close: () => null } }));
vi.mock("@bisheng/ui", () => ({
    Button: ({ children, onClick, disabled, "aria-label": ariaLabel }: { children?: unknown; onClick?: () => void; disabled?: boolean; "aria-label"?: string }) => (
        <button type="button" onClick={onClick} disabled={disabled} aria-label={ariaLabel}>
            {children as never}
        </button>
    ),
}));
vi.mock("@bisheng/chat", () => ({
    ConversationComposer: (props: {
        inputRef: { current: HTMLTextAreaElement | null };
        canExecute: boolean;
        showStop?: boolean;
        onSend: () => void;
        onStop?: () => void;
    }) => (
        <div>
            <textarea aria-label="composer" ref={props.inputRef} disabled={!props.canExecute} />
            <button type="button" aria-label="send" disabled={!props.canExecute} onClick={() => void props.onSend()} />
            {props.showStop && <button type="button" aria-label="stop" onClick={() => void props.onStop?.()} />}
        </div>
    ),
}));

import CopilotDrawer, { type CanvasState } from "./CopilotDrawer";

const PROPOSED = { nodes: [{ id: "new", position: { x: 1, y: 1 }, data: { type: "llm", name: "summary" } }], edges: [] };

function proposalEvents(): CopilotEvent[] {
    return [
        { type: "status", stage: "generating", attempt: 1 },
        { type: "message", text: "Added a summary node." },
        {
            type: "proposal",
            canvas: PROPOSED,
            diff: { identical: false, nodes_only_local: [{ id: "new", type: "llm", name: "summary" }], nodes_only_online: [], nodes_changed: [], edges_only_local: [], edges_only_online: [] },
            needs_selection: [{ node_id: "new", param_key: "model_id", kind: "model", wanted: "gpt-9", candidates: ["qwen"] }],
            replaced: [],
        },
        { type: "done" },
    ];
}

function streamOf(events: CopilotEvent[]) {
    return async function* () {
        for (const event of events) yield event;
    };
}

let canvas: CanvasState;
const getCanvas = () => canvas;

function renderDrawer(overrides: Partial<Parameters<typeof CopilotDrawer>[0]> = {}) {
    const props = { open: true, flowId: "f1", getCanvas, onApply: vi.fn(), onClose: vi.fn(), ...overrides };
    const view = render(<CopilotDrawer {...props} />);
    return { ...view, props };
}

async function send(text: string) {
    const box = screen.getByLabelText("composer") as HTMLTextAreaElement;
    await waitFor(() => expect(box.disabled).toBe(false));
    box.value = text;
    await act(async () => {
        fireEvent.click(screen.getByLabelText("send"));
    });
}

beforeEach(() => {
    canvas = {
        nodes: [{ id: "start_1", position: { x: 0, y: 0 }, data: { type: "start" } }],
        edges: [],
    } as unknown as CanvasState;
    mocks.check.mockResolvedValue(undefined);
});
afterEach(() => {
    cleanup();
    vi.clearAllMocks();
});

describe("availability (AC-03)", () => {
    it("shows the reason and keeps sending disabled", async () => {
        mocks.check.mockRejectedValueOnce(new Error("model not configured"));
        renderDrawer();
        expect(await screen.findByTestId("copilot-unavailable")).toHaveTextContent("model not configured");
        expect((screen.getByLabelText("send") as HTMLButtonElement).disabled).toBe(true);
        expect(mocks.stream).not.toHaveBeenCalled();
    });
});

describe("a turn", () => {
    it("sends the canvas as it is now and shows the proposal with what needs a choice", async () => {
        mocks.stream.mockImplementation(streamOf(proposalEvents()));
        renderDrawer();
        await send("add a summary node");

        const [request] = mocks.stream.mock.calls[0] as [CopilotRequest, AbortSignal];
        expect(request).toMatchObject({ flow_id: "f1", message: "add a summary node", history: [] });
        expect(request.canvas.nodes).toEqual(canvas.nodes);
        expect(await screen.findByTestId("copilot-diff")).toHaveTextContent("summary");
        expect(screen.getByTestId("copilot-needs")).toBeInTheDocument();
    });

    it("applies the merged canvas when the canvas did not change", async () => {
        mocks.stream.mockImplementation(streamOf(proposalEvents()));
        const { props } = renderDrawer();
        await send("add a summary node");
        fireEvent.click(await screen.findByText("Apply"));
        expect(mocks.confirm).not.toHaveBeenCalled();
        expect(props.onApply).toHaveBeenCalledWith({ nodes: PROPOSED.nodes, edges: PROPOSED.edges });
        expect(screen.getByTestId("copilot-outcome")).toHaveTextContent("Applied");
    });

    it("selecting a node after sending is not an edit", async () => {
        mocks.stream.mockImplementation(streamOf(proposalEvents()));
        const { props } = renderDrawer();
        await send("add a summary node");
        canvas = { ...canvas, nodes: canvas.nodes.map((n) => ({ ...n, selected: true })) };
        fireEvent.click(await screen.findByText("Apply"));
        expect(mocks.confirm).not.toHaveBeenCalled();
        expect(props.onApply).toHaveBeenCalled();
    });

    it("asks before overwriting edits made after sending, and cancelling changes nothing (AC-19)", async () => {
        mocks.stream.mockImplementation(streamOf(proposalEvents()));
        const { props } = renderDrawer();
        await send("add a summary node");
        canvas = { ...canvas, nodes: canvas.nodes.map((n) => ({ ...n, position: { x: 50, y: 0 } })) };
        fireEvent.click(await screen.findByText("Apply"));

        expect(mocks.confirm).toHaveBeenCalledTimes(1);
        expect(props.onApply).not.toHaveBeenCalled();
        const { onOk } = mocks.confirm.mock.calls[0][0] as { onOk: (next: () => void) => void };
        act(() => onOk(() => undefined));
        expect(props.onApply).toHaveBeenCalledTimes(1);
    });

    it("discarding leaves the canvas alone (AC-20)", async () => {
        mocks.stream.mockImplementation(streamOf(proposalEvents()));
        const { props } = renderDrawer();
        await send("add a summary node");
        fireEvent.click(await screen.findByText("Discard"));
        expect(props.onApply).not.toHaveBeenCalled();
        expect(screen.queryByText("Apply")).toBeNull();
    });

    it("shows issues and errors instead of a proposal", async () => {
        mocks.stream.mockImplementation(
            streamOf([
                { type: "message", text: "Tried." },
                { type: "issues", issues: ["node a lacks a user prompt"] },
                { type: "done" },
            ]),
        );
        renderDrawer();
        await send("x");
        expect(await screen.findByTestId("copilot-issues")).toHaveTextContent("node a lacks a user prompt");
        expect(screen.queryByText("Apply")).toBeNull();
    });

    it("lists each changed parameter as old value → new value (AC-17)", async () => {
        const events = proposalEvents();
        const proposal = events[2] as Extract<CopilotEvent, { type: "proposal" }>;
        proposal.canvas = { nodes: [{ id: "start_1", position: { x: 0, y: 0 }, data: { type: "start", name: "start" } }], edges: [] };
        proposal.diff = {
            ...proposal.diff,
            nodes_only_local: [],
            nodes_changed: [{ id: "start_1", fields: [], params: [{ key: "guide_word", online: "", local: "hello" }] }],
        };
        proposal.needs_selection = [{ node_id: "tool_1", param_key: "tool", kind: "tool_unchanged", wanted: "send_mail", candidates: [] }];
        mocks.stream.mockImplementation(streamOf(events));
        renderDrawer();
        await send("greet");
        const change = await screen.findByTestId("copilot-change");
        expect(change).toHaveTextContent("guide_word: (empty) → hello");
        expect(screen.getByTestId("copilot-needs")).toHaveTextContent("tool_1: send_mail");
    });

    it("a retry replaces the rejected attempt's streamed text", async () => {
        mocks.stream.mockImplementation(
            streamOf([
                { type: "status", stage: "generating", attempt: 1 },
                { type: "delta", text: "first try" },
                { type: "status", stage: "retrying", attempt: 2 },
                { type: "delta", text: "second try" },
                { type: "issues", issues: ["still wrong"] },
                { type: "done" },
            ]),
        );
        renderDrawer();
        await send("x");
        await screen.findByTestId("copilot-issues");
        expect(screen.queryByText(/first try/)).toBeNull();
        expect(screen.getByText(/second try/)).toBeInTheDocument();
    });

    it("the next turn carries the earlier text, not the proposal (AC-07)", async () => {
        mocks.stream.mockImplementation(streamOf(proposalEvents()));
        renderDrawer();
        await send("first");
        await screen.findByTestId("copilot-diff");
        await send("second");
        const [request] = mocks.stream.mock.calls[1] as [CopilotRequest, AbortSignal];
        expect(request.history).toEqual([
            { role: "user", content: "first" },
            { role: "assistant", content: "Added a summary node." },
        ]);
    });
});

describe("stop and keep", () => {
    it("stop aborts the request and nothing is proposed (AC-11)", async () => {
        let seenSignal: AbortSignal | undefined;
        mocks.stream.mockImplementation(async function* (_request: CopilotRequest, signal: AbortSignal) {
            seenSignal = signal;
            yield { type: "status", stage: "generating", attempt: 1 } as CopilotEvent;
            await new Promise((_, reject) => signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
        });
        const { props } = renderDrawer();
        await send("x");
        fireEvent.click(await screen.findByLabelText("stop"));
        await waitFor(() => expect(screen.getByTestId("copilot-outcome")).toHaveTextContent("Stopped"));
        expect(seenSignal?.aborted).toBe(true);
        expect(props.onApply).not.toHaveBeenCalled();
    });

    it("closing and reopening the drawer keeps the conversation (AC-21)", async () => {
        mocks.stream.mockImplementation(streamOf(proposalEvents()));
        const { rerender, props } = renderDrawer();
        await send("keep me");
        await screen.findByTestId("copilot-diff");
        rerender(<CopilotDrawer {...props} open={false} />);
        rerender(<CopilotDrawer {...props} open />);
        expect(screen.getByTestId("copilot-user")).toHaveTextContent("keep me");
    });
});
