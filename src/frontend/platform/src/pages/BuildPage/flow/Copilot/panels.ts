/**
 * F007 editor wiring that is worth testing on its own (design decisions 10 and 11).
 *
 * The copilot drawer and the test-run panel share the right edge of the canvas, so
 * only one is open at a time. Applying a proposal must be one undo step, so the
 * snapshot is taken before the canvas changes (the existing import path skips it).
 */

/** Open or close the copilot drawer; opening it closes the test-run panel. */
export function setCopilotPanel(open: boolean, closeTestPanel: () => void, setCopilotOpen: (open: boolean) => void): void {
    if (open) closeTestPanel();
    setCopilotOpen(open);
}

/** The test-run panel reports its state; opening it closes the copilot drawer. */
export function onTestPanelOpenChange(open: boolean, setCopilotOpen: (open: boolean) => void): void {
    if (open) setCopilotOpen(false);
}

export interface CanvasWriter<N, E> {
    takeSnapshot: () => void;
    setNodes: (nodes: N[]) => void;
    setEdges: (edges: E[]) => void;
}

/** Land a proposal: snapshot first so one Ctrl+Z restores the canvas as it was (AC-18). Never saves. */
export function applyCanvas<N, E>(writer: CanvasWriter<N, E>, canvas: { nodes: N[]; edges: E[] }): void {
    writer.takeSnapshot();
    writer.setNodes(canvas.nodes);
    writer.setEdges(canvas.edges);
}
