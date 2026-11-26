import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CopilotEvent, CopilotRequest } from "@/controllers/API/workflowCopilot";

// F007 T019: the drawer's own behaviour, with the transport, the confirm dialog and the
// shared composer replaced by small stand-ins.

const mocks = vi.hoisted(() => ({
    check: vi.fn(),
    stream: vi.fn(),
    confirm: vi.fn(),
    translate: vi.fn((key: string) => key),
    scroll: vi.fn(),
}));

vi.mock("@/controllers/API/workflowCopilot", () => ({
    checkCopilotAvailability: mocks.check,
    streamCopilot: mocks.stream,
}));
vi.mock("@/components/bs-ui/alertDialog/useConfirm", () => ({ bsConfirm: mocks.confirm }));
vi.mock("react-i18next", () => ({ useTranslation: () => ({ t: (key: string) => mocks.translate(key) }) }));
vi.mock("bisheng-icons", () => ({ Outlined: new Proxy({}, { get: () => () => null }) }));
vi.mock("@/components/bs-comp/appChat/PlatformChatMarkdown", () => ({
    PlatformChatMarkdown: ({ content }: { content: string }) => <div>{content}</div>,
}));
vi.mock("@bisheng/ui", () => ({
    Button: ({ children, onClick, disabled, "aria-label": ariaLabel }: { children?: unknown; onClick?: () => void; disabled?: boolean; "aria-label"?: string }) => (
        <button type="button" onClick={onClick} disabled={disabled} aria-label={ariaLabel}>
            {children as never}
        </button>
    ),
}));
vi.mock("@bisheng/chat", () => ({
    useConversationScroll: mocks.scroll,
    MessageBubble: (props: { reasoning?: unknown; children?: unknown; status?: unknown; pending?: boolean; hideBody?: boolean }) => (
        <div>
            {props.reasoning as never}
            {!props.hideBody && (props.children as never)}
            {!props.hideBody && props.pending && <span data-testid="bubble-pending" />}
            {props.status as never}
        </div>
    ),
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
import { endCopilotSession } from "./session";

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
    mocks.translate.mockImplementation((key: string) => key);
});
afterEach(() => {
    cleanup();
    endCopilotSession(); // the session outlives the component, as it does in the editor
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

    it("discarding leaves the canvas alone (AC-20) and says so in the progress", async () => {
        mocks.stream.mockImplementation(streamOf([proposalEvents()[0], { type: "draft", text: "nodes: []\n" }, ...proposalEvents().slice(1)]));
        const { props } = renderDrawer();
        await send("add a summary node");
        fireEvent.click(await screen.findByText("Discard"));
        expect(props.onApply).not.toHaveBeenCalled();
        expect(screen.queryByText("Apply")).toBeNull();
        expect(screen.getByTestId("copilot-progress-title")).toHaveTextContent("Discarded");
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

    it("a retry replaces the rejected attempt's streamed text and is listed in the progress", async () => {
        mocks.stream.mockImplementation(
            streamOf([
                { type: "status", stage: "generating", attempt: 1 },
                { type: "delta", text: "first try" },
                { type: "draft", text: "nodes: [broken" },
                { type: "status", stage: "retrying", attempt: 2 },
                { type: "delta", text: "second try" },
                { type: "draft", text: "nodes: []\n" },
                { type: "issues", issues: ["still wrong"] },
                { type: "done" },
            ]),
        );
        renderDrawer();
        await send("x");
        await screen.findByTestId("copilot-issues");
        expect(screen.queryByText(/first try/)).toBeNull();
        expect(screen.getByText(/second try/)).toBeInTheDocument();
        expect(screen.getByTestId("copilot-progress-title")).toHaveTextContent("The proposal did not validate");
        fireEvent.click(screen.getByRole("button", { expanded: false }));
        expect(screen.getByTestId("copilot-progress")).toHaveTextContent("#1"); // the rejected attempt, by its fallback copy
        expect(screen.getByTestId("copilot-draft")).toHaveTextContent("nodes: []");
        expect(screen.queryByText(/broken/)).toBeNull();
    });

    it("a failed turn shows the error code's localized copy with the exception class (AC-27)", async () => {
        mocks.translate.mockImplementation((key: string) => (key === "api_errors:10563" ? "LOCALIZED-10563" : key));
        mocks.stream.mockImplementation(
            streamOf([
                { type: "error", code: 10563, message: "The copilot model call failed（APIConnectionError）" },
                { type: "done" },
            ]),
        );
        renderDrawer();
        await send("x");
        const error = await screen.findByTestId("copilot-error");
        expect(error).toHaveTextContent("LOCALIZED-10563");
        expect(error).toHaveTextContent("APIConnectionError");
        expect(error).not.toHaveTextContent("The copilot model call failed");
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

/** A stream that yields ``before``, then waits for ``release()`` before yielding ``after``. */
function gatedStream(before: CopilotEvent[], after: CopilotEvent[]) {
    let release!: () => void;
    const gate = new Promise<void>((resolve) => (release = resolve));
    mocks.stream.mockImplementation(async function* () {
        for (const event of before) yield event;
        await gate;
        for (const event of after) yield event;
    });
    return () => act(async () => release());
}

describe("progress while generating (chat-style)", () => {
    it("shows the spinner bubble until the first status arrives", async () => {
        const release = gatedStream([], [{ type: "done" }]);
        renderDrawer();
        await send("x");
        expect(screen.getByTestId("bubble-pending")).toBeInTheDocument();
        await release();
        expect(screen.queryByTestId("bubble-pending")).toBeNull();
    });

    it("streams the YAML into an open box, then folds it away with the proposal", async () => {
        const [status, message, proposal, done] = proposalEvents();
        const release = gatedStream(
            [status, { type: "delta", text: "I will add a node." }, { type: "draft", text: "nodes:\n" }, { type: "draft", text: "  - id: new\n" }],
            [{ type: "status", stage: "validating", attempt: 1 }, message, proposal, done],
        );
        renderDrawer();
        await send("x");
        expect(await screen.findByTestId("copilot-draft")).toHaveTextContent("- id: new");
        expect(screen.getByTestId("copilot-progress-title")).toHaveTextContent("Generating…");
        expect(screen.getByText("I will add a node.")).toBeInTheDocument();

        await release();
        await screen.findByTestId("copilot-diff");
        expect(screen.getByTestId("copilot-progress-title")).toHaveTextContent("Proposal ready");
        expect(screen.queryByTestId("copilot-draft")).toBeNull(); // folded
        fireEvent.click(screen.getByRole("button", { expanded: false }));
        expect(screen.getByTestId("copilot-draft")).toHaveTextContent("- id: new"); // still there to read
    });

    it("keeps the rejected attempt's text on screen until the retry says something", async () => {
        const release = gatedStream(
            [
                { type: "status", stage: "generating", attempt: 1 },
                { type: "delta", text: "first try" },
                { type: "status", stage: "retrying", attempt: 2 },
            ],
            [{ type: "delta", text: "second try" }, { type: "issues", issues: ["no"] }, { type: "done" }],
        );
        renderDrawer();
        await send("x");
        expect(await screen.findByText("first try")).toBeInTheDocument();
        expect(screen.getByTestId("copilot-progress-title")).toHaveTextContent("#2");
        await release();
        expect(await screen.findByText("second try")).toBeInTheDocument();
        expect(screen.queryByText("first try")).toBeNull();
    });

    it("folds when the run ends even after being toggled by hand", async () => {
        const [status, message, proposal, done] = proposalEvents();
        const release = gatedStream([status, { type: "draft", text: "nodes: []\n" }], [message, proposal, done]);
        renderDrawer();
        await send("x");
        await screen.findByTestId("copilot-draft");
        fireEvent.click(screen.getByRole("button", { expanded: true })); // closed by hand
        fireEvent.click(screen.getByRole("button", { expanded: false })); // and opened again
        await release();
        await screen.findByTestId("copilot-diff");
        expect(screen.queryByTestId("copilot-draft")).toBeNull();
    });

    it("a reply still running counts as the user's, so sending resumes following", async () => {
        const release = gatedStream([{ type: "status", stage: "generating", attempt: 1 }], [{ type: "message", text: "ok" }, { type: "done" }]);
        renderDrawer();
        await send("x");
        const kinds = () => (mocks.scroll.mock.lastCall?.[0] as { messages: { kind: string }[] }).messages.map((m) => m.kind);
        expect(kinds()).toEqual(["user", "user"]);
        await release();
        await waitFor(() => expect(kinds()).toEqual(["user", "answer"]));
    });

    it("a stopped turn's half-streamed text is not sent as history", async () => {
        mocks.stream.mockImplementationOnce(async function* (_request: CopilotRequest, signal: AbortSignal) {
            yield { type: "status", stage: "generating", attempt: 1 } as CopilotEvent;
            yield { type: "delta", text: "half a sent" } as CopilotEvent;
            await new Promise((_, reject) => signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
        });
        renderDrawer();
        await send("first");
        await screen.findByText("half a sent");
        fireEvent.click(screen.getByLabelText("stop"));
        await screen.findByTestId("copilot-outcome");
        mocks.stream.mockImplementation(streamOf(proposalEvents()));
        await send("second");
        const [request] = mocks.stream.mock.calls[1] as [CopilotRequest, AbortSignal];
        expect(request.history).toEqual([{ role: "user", content: "first" }]);
    });

    it("shows the model's thinking while it reasons, then the YAML under it", async () => {
        const [status, message, proposal, done] = proposalEvents();
        const release = gatedStream(
            [status, { type: "reasoning", text: "Think first: " }, { type: "reasoning", text: "how many nodes." }],
            [{ type: "draft", text: "nodes: []\n" }, message, proposal, done],
        );
        renderDrawer();
        await send("x");
        expect(await screen.findByTestId("copilot-reasoning")).toHaveTextContent("Think first: how many nodes.");
        expect(screen.getByTestId("copilot-progress-title")).toHaveTextContent("Thinking…");
        await release();
        await screen.findByTestId("copilot-diff");
        expect(screen.getByTestId("copilot-progress-title")).toHaveTextContent("Proposal ready");
        fireEvent.click(screen.getByRole("button", { expanded: false }));
        expect(screen.getByTestId("copilot-reasoning")).toHaveTextContent("Think first: how many nodes.");
        expect(screen.getByTestId("copilot-draft")).toHaveTextContent("nodes: []");
    });

    it("a text answer that came with thinking keeps a folded 'thought' box, and the thinking is not history", async () => {
        mocks.stream.mockImplementation(
            streamOf([
                { type: "status", stage: "generating", attempt: 1 },
                { type: "reasoning", text: "regex matches partially" },
                { type: "message", text: "It is a partial match." },
                { type: "done" },
            ]),
        );
        renderDrawer();
        await send("is regex a full match?");
        await screen.findByText("It is a partial match.");
        expect(screen.getByTestId("copilot-progress-title")).toHaveTextContent("Thought it through");
        expect(screen.queryByTestId("copilot-reasoning")).toBeNull(); // folded
        mocks.stream.mockImplementation(streamOf(proposalEvents()));
        await send("next");
        const [request] = mocks.stream.mock.calls[1] as [CopilotRequest, AbortSignal];
        expect(JSON.stringify(request.history)).not.toContain("regex matches partially");
    });

    it("a retry that is still thinking keeps saying which attempt it is", async () => {
        const release = gatedStream(
            [
                { type: "status", stage: "generating", attempt: 1 },
                { type: "draft", text: "nodes: [" },
                { type: "status", stage: "retrying", attempt: 2 },
                { type: "reasoning", text: "Fix the broken list." },
            ],
            [{ type: "issues", issues: ["still wrong"] }, { type: "done" }],
        );
        renderDrawer();
        await send("x");
        expect(await screen.findByTestId("copilot-reasoning")).toHaveTextContent("Fix the broken list.");
        expect(screen.getByTestId("copilot-progress-title")).toHaveTextContent("#2");
        await release();
    });

    it("a plain text answer leaves no progress box behind", async () => {
        mocks.stream.mockImplementation(
            streamOf([
                { type: "status", stage: "generating", attempt: 1 },
                { type: "delta", text: "Regex is a partial match." },
                { type: "message", text: "Regex is a partial match." },
                { type: "done" },
            ]),
        );
        renderDrawer();
        await send("is regex a full match?");
        await waitFor(() => expect(screen.queryByTestId("copilot-progress")).toBeNull());
        expect(screen.getByText("Regex is a partial match.")).toBeInTheDocument();
    });
});

describe("stop and keep", () => {
    it("stop aborts the request and nothing is proposed (AC-11)", async () => {
        let seenSignal: AbortSignal | undefined;
        mocks.stream.mockImplementation(async function* (_request: CopilotRequest, signal: AbortSignal) {
            seenSignal = signal;
            yield { type: "status", stage: "generating", attempt: 1 } as CopilotEvent;
            yield { type: "draft", text: "nodes:\n" } as CopilotEvent;
            await new Promise((_, reject) => signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
        });
        const { props } = renderDrawer();
        await send("x");
        fireEvent.click(await screen.findByLabelText("stop"));
        await waitFor(() => expect(screen.getByTestId("copilot-outcome")).toHaveTextContent("Stopped"));
        expect(screen.getByTestId("copilot-progress-title")).toHaveTextContent("Stopped");
        expect(seenSignal?.aborted).toBe(true);
        expect(props.onApply).not.toHaveBeenCalled();
    });

    it("leaving the editor mid-turn aborts the request", async () => {
        let seenSignal: AbortSignal | undefined;
        mocks.stream.mockImplementation(async function* (_request: CopilotRequest, signal: AbortSignal) {
            seenSignal = signal;
            yield { type: "status", stage: "generating", attempt: 1 } as CopilotEvent;
            await new Promise((_, reject) => signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
        });
        const { unmount } = renderDrawer();
        await send("x");
        await waitFor(() => expect(seenSignal).toBeDefined());
        unmount();
        expect(seenSignal?.aborted).toBe(false); // the editor re-mounting (Save does) is not leaving
        act(() => endCopilotSession());
        expect(seenSignal?.aborted).toBe(true);
    });

    it("the editor re-mounting (Save) keeps the conversation and a turn still streaming", async () => {
        let release: () => void = () => undefined;
        const [status, message, proposal, done] = proposalEvents();
        mocks.stream.mockImplementation(async function* () {
            yield status;
            await new Promise<void>((resolve) => (release = resolve));
            yield message;
            yield proposal;
            yield done;
        });
        const first = renderDrawer();
        await send("keep me");
        first.unmount();
        renderDrawer();
        expect(screen.getByTestId("copilot-user")).toHaveTextContent("keep me");
        await act(async () => release());
        expect(await screen.findByTestId("copilot-diff")).toBeInTheDocument();
    });

    it("another workflow starts a fresh conversation", async () => {
        mocks.stream.mockImplementation(streamOf(proposalEvents()));
        const { rerender, props } = renderDrawer();
        await send("for f1");
        await screen.findByTestId("copilot-diff");
        rerender(<CopilotDrawer {...props} flowId="f2" />);
        await waitFor(() => expect(screen.queryByTestId("copilot-user")).toBeNull());
    });

    it("stopping after the proposal arrived keeps it choosable", async () => {
        const [status, message, proposal] = proposalEvents();
        mocks.stream.mockImplementation(async function* (_request: CopilotRequest, signal: AbortSignal) {
            yield status;
            yield message;
            yield proposal;
            await new Promise((_, reject) => signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError"))));
        });
        renderDrawer();
        await send("x");
        await screen.findByTestId("copilot-diff");
        fireEvent.click(screen.getByLabelText("stop"));
        expect(await screen.findByText("Apply")).toBeInTheDocument();
        expect(screen.queryByTestId("copilot-outcome")).toBeNull();
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
