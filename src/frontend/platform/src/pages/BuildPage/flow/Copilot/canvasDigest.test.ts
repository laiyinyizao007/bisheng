import { expect, it } from "vitest";
import { canvasDigest } from "./canvasDigest";

// F007 AC-19 / design pitfall 6: selection is not an edit; a drag or a parameter change is.

const nodes = () => [
    { id: "a", type: "flowNode", position: { x: 0, y: 0 }, data: { name: "start", group_params: [{ params: [{ key: "k", value: 1 }] }] } },
    { id: "b", type: "flowNode", position: { x: 300, y: 0 }, data: { name: "llm" } },
];
const edges = () => [{ id: "e1", source: "a", sourceHandle: "right_handle", target: "b", targetHandle: "left_handle", animated: true }];

it("ignores React Flow runtime state", () => {
    const base = canvasDigest(nodes(), edges());
    const touched = nodes().map((n) => ({ ...n, selected: true, dragging: false, measured: { width: 320, height: 180 }, width: 320 }));
    const touchedEdges = edges().map((e) => ({ ...e, selected: true }));
    expect(canvasDigest(touched, touchedEdges)).toBe(base);
});

it("ignores order of nodes, edges and data keys", () => {
    const reordered = nodes().reverse().map((n) => ({ ...n, data: Object.fromEntries(Object.entries(n.data).reverse()) }));
    expect(canvasDigest(reordered, edges())).toBe(canvasDigest(nodes(), edges()));
});

it("sees a drag, a parameter edit and a new edge", () => {
    const base = canvasDigest(nodes(), edges());
    const dragged = nodes();
    dragged[1].position = { x: 310, y: 0 };
    expect(canvasDigest(dragged, edges())).not.toBe(base);

    const edited = nodes();
    (edited[0].data as { group_params: { params: { value: number }[] }[] }).group_params[0].params[0].value = 2;
    expect(canvasDigest(edited, edges())).not.toBe(base);

    expect(canvasDigest(nodes(), [...edges(), { id: "e2", source: "b", sourceHandle: "right_handle", target: "a", targetHandle: "left_handle", animated: true }])).not.toBe(base);
});
