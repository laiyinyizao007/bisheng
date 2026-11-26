import { expect, it, vi } from "vitest";
import { applyCanvas, onTestPanelOpenChange, setCopilotPanel } from "./panels";

// F007 T018: one panel on the right edge at a time; applying is one undo step.

it("opening the copilot closes the test-run panel; closing it leaves the panel alone", () => {
    const closeTest = vi.fn();
    const setOpen = vi.fn();
    setCopilotPanel(true, closeTest, setOpen);
    expect(closeTest).toHaveBeenCalledTimes(1);
    expect(setOpen).toHaveBeenLastCalledWith(true);

    setCopilotPanel(false, closeTest, setOpen);
    expect(closeTest).toHaveBeenCalledTimes(1);
    expect(setOpen).toHaveBeenLastCalledWith(false);
});

it("opening the test-run panel closes the copilot; its closing does not reopen it", () => {
    const setOpen = vi.fn();
    onTestPanelOpenChange(true, setOpen);
    expect(setOpen).toHaveBeenCalledWith(false);
    setOpen.mockClear();
    onTestPanelOpenChange(false, setOpen);
    expect(setOpen).not.toHaveBeenCalled();
});

it("takes the undo snapshot before the canvas changes", () => {
    const order: string[] = [];
    let current = { nodes: ["old"], edges: ["e-old"] };
    const snapshots: (typeof current)[] = [];
    applyCanvas(
        {
            takeSnapshot: () => {
                order.push("snapshot");
                snapshots.push({ ...current });
            },
            setNodes: (nodes) => {
                order.push("nodes");
                current = { ...current, nodes };
            },
            setEdges: (edges) => {
                order.push("edges");
                current = { ...current, edges };
            },
        },
        { nodes: ["new"], edges: ["e-new"] },
    );
    expect(order).toEqual(["snapshot", "nodes", "edges"]);
    expect(snapshots).toEqual([{ nodes: ["old"], edges: ["e-old"] }]); // what one undo restores
    expect(current).toEqual({ nodes: ["new"], edges: ["e-new"] });
});
