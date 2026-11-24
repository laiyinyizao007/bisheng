import { afterEach, expect, it, vi } from "vitest";
import request from "../request";
import { requestStream } from "../requestStream";
import { checkCopilotAvailability, readNdjson, streamCopilot } from "./workflowCopilot";

// F007 T017: the copilot request goes through the shared stream helper with its JSON body.
vi.mock("../request", () => ({ default: { get: vi.fn() } }));
vi.mock("../requestStream", () => ({ requestStream: vi.fn() }));
afterEach(() => vi.clearAllMocks());

function streamOf(...chunks: string[]): ReadableStream<Uint8Array> {
    const encoder = new TextEncoder();
    return new ReadableStream({
        start(controller) {
            for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
            controller.close();
        },
    });
}

async function collect<T>(source: AsyncGenerator<T>): Promise<T[]> {
    const out: T[] = [];
    for await (const item of source) out.push(item);
    return out;
}

it("posts the request body to the copilot endpoint and yields its events", async () => {
    vi.mocked(requestStream).mockResolvedValueOnce(streamOf('{"type":"status","stage":"generating","attempt":1}\n{"type":"done"}\n'));
    const payload = { flow_id: "f1", canvas: { nodes: [], edges: [] }, history: [], message: "加一个节点" };
    const signal = new AbortController().signal;

    const events = await collect(streamCopilot(payload, signal));

    expect(requestStream).toHaveBeenCalledWith("/api/v1/workflow/copilot", signal, payload);
    expect(events.map((e) => e.type)).toEqual(["status", "done"]);
});

it("reassembles lines split across chunks, including multi-byte characters", async () => {
    const line = JSON.stringify({ type: "message", text: "已加入摘要节点" });
    const bytes = new TextEncoder().encode(line + "\n" + '{"type":"done"}');
    const cut = 5 + new TextEncoder().encode('{"type":"message","text":"已').length; // inside a 3-byte character
    const reader = new ReadableStream<Uint8Array>({
        start(controller) {
            controller.enqueue(bytes.slice(0, cut));
            controller.enqueue(bytes.slice(cut));
            controller.close();
        },
    });
    const events = await collect(readNdjson(reader));
    expect(events).toEqual([{ type: "message", text: "已加入摘要节点" }, { type: "done" }]);
});

it("checks availability silently so the drawer can show the reason itself", async () => {
    vi.mocked(request.get).mockResolvedValueOnce({ available: true });
    await checkCopilotAvailability("f1");
    expect(request.get).toHaveBeenCalledWith("/api/v1/workflow/copilot/availability", { params: { flow_id: "f1" }, silent: true });
});

it("rejects when the copilot is unavailable", async () => {
    const failure = Object.assign(new Error("未配置工作流默认模型"), { status_code: 10560 });
    vi.mocked(request.get).mockRejectedValueOnce(failure);
    await expect(checkCopilotAvailability("f1")).rejects.toBe(failure);
});
