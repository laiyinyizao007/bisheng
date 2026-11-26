import type { Edge, Node } from "@xyflow/react";
import { describe, expect, it } from "vitest";
import { autoLayout, collapsed, readyToLayout, sizeKey, sizeSettler } from "./autoLayout";

function node(id: string, type = "llm", size?: { width: number; height: number }): Node {
    return {
        id,
        type: "flowNode",
        position: { x: 0, y: 0 },
        data: { type },
        ...(size ? { measured: size } : {}),
    } as Node;
}
const edge = (source: string, target: string, sourceHandle = "right_handle"): Edge => ({ id: `${source}-${target}-${sourceHandle}`, source, target, sourceHandle }) as Edge;
const at = (nodes: Node[], id: string) => nodes.find((n) => n.id === id)!.position;
const SIZE = { width: 300, height: 200 };

function overlaps(nodes: Node[]): string[] {
    const boxes = nodes.map((n) => ({ id: n.id, ...n.position, w: n.measured?.width ?? 0, h: n.measured?.height ?? 0 }));
    const found: string[] = [];
    for (let i = 0; i < boxes.length; i++)
        for (let j = i + 1; j < boxes.length; j++) {
            const a = boxes[i];
            const b = boxes[j];
            if (a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h) found.push(`${a.id}/${b.id}`);
        }
    return found;
}

describe("readyToLayout (the applied canvas has landed and been measured)", () => {
    const applied = [node("s"), node("new")];
    it("waits while React Flow still shows the old, measured canvas", () => {
        expect(readyToLayout([node("s", "llm", SIZE), node("old", "llm", SIZE)], applied)).toBe(false);
    });
    it("waits until every applied node is measured", () => {
        expect(readyToLayout([node("s", "llm", SIZE), node("new")], applied)).toBe(false);
        expect(readyToLayout([node("s", "llm", SIZE), node("new", "llm", SIZE)], applied)).toBe(true);
    });
    it("does not wait on notes, which are not laid out", () => {
        const note = { ...node("n"), type: "noteNode" } as Node;
        expect(readyToLayout([node("s", "llm", SIZE), note], [node("s"), note])).toBe(true);
    });
});

describe("autoLayout (layered, left to right)", () => {
    it("lays a chain out left to right on one line", () => {
        const nodes = ["start", "a", "b", "end"].map((id) => node(id, id === "start" || id === "end" ? id : "llm", SIZE));
        const laid = autoLayout(nodes, [edge("start", "a"), edge("a", "b"), edge("b", "end")]);
        const xs = ["start", "a", "b", "end"].map((id) => at(laid, id).x);
        expect(xs).toEqual([...xs].sort((p, q) => p - q));
        expect(new Set(xs).size).toBe(4);
        expect(new Set(["start", "a", "b", "end"].map((id) => at(laid, id).y)).size).toBe(1);
    });

    it("puts branches of a condition in the same column, stacked without overlap", () => {
        const nodes = [node("start", "start", SIZE), node("cond", "condition", SIZE), node("yes", "llm", SIZE), node("no", "llm", SIZE), node("end", "end", SIZE)];
        const edges = [edge("start", "cond"), edge("cond", "yes", "case1"), edge("cond", "no"), edge("yes", "end"), edge("no", "end")];
        const laid = autoLayout(nodes, edges);
        expect(at(laid, "yes").x).toBe(at(laid, "no").x);
        expect(at(laid, "yes").y).not.toBe(at(laid, "no").y);
        expect(at(laid, "end").x).toBeGreaterThan(at(laid, "yes").x);
        expect(overlaps(laid)).toEqual([]);
    });

    it("a loop back to an earlier node does not drag that node to the right", () => {
        const nodes = ["start", "input", "llm", "output"].map((id) => node(id, id, SIZE));
        const edges = [edge("start", "input"), edge("input", "llm"), edge("llm", "output"), edge("output", "input")];
        const laid = autoLayout(nodes, edges);
        expect(at(laid, "input").x).toBeLessThan(at(laid, "llm").x);
        expect(at(laid, "llm").x).toBeLessThan(at(laid, "output").x);
    });

    it("leaves notes where they are and keeps every other field", () => {
        const note = { ...node("note"), type: "noteNode", position: { x: 999, y: 7 } } as Node;
        const llm = { ...node("a", "llm", SIZE), selected: true } as Node;
        const laid = autoLayout([note, llm], []);
        expect(at(laid, "note")).toEqual({ x: 999, y: 7 });
        expect(laid.find((n) => n.id === "a")).toMatchObject({ selected: true, data: { type: "llm" } });
    });

    it("columns clear the widest node of the previous column, unmeasured nodes included", () => {
        const nodes = [node("start", "start", { width: 500, height: 100 }), node("x", "llm"), node("y", "llm")];
        const laid = autoLayout(nodes, [edge("start", "x"), edge("x", "y")]);
        expect(at(laid, "x").x).toBeGreaterThanOrEqual(at(laid, "start").x + 500);
        expect(at(laid, "y").x).toBeGreaterThan(at(laid, "x").x + 300); // estimated width, not zero
    });

    it("is deterministic and ignores edges to missing nodes", () => {
        const nodes = ["s", "a", "b", "c"].map((id) => node(id, "llm", SIZE));
        const edges = [edge("s", "a"), edge("s", "b"), edge("s", "c"), edge("a", "ghost")];
        expect(autoLayout(nodes, edges)).toEqual(autoLayout(nodes, edges));
        expect(overlaps(autoLayout(nodes, edges))).toEqual([]);
    });

    it("a size that is not a finite positive number falls back to the estimate", () => {
        const nodes = [node("s", "start", { width: Number.NaN, height: Infinity }), node("a", "llm", { width: 0, height: 200 }), node("b", "llm", SIZE)];
        const laid = autoLayout(nodes, [edge("s", "a"), edge("a", "b")]);
        laid.forEach((n) => expect(Number.isFinite(n.position.x) && Number.isFinite(n.position.y)).toBe(true));
        expect(at(laid, "b").x).toBeGreaterThan(at(laid, "a").x + 300);
    });

    it("ids containing '->' do not confuse back edges with forward ones", () => {
        const ids = ["start", "a->b", "c", "a", "b->c"];
        const nodes = ids.map((id) => node(id, id === "start" ? "start" : "llm", SIZE));
        // Loop start → c → a->b → c: its closing edge (a->b, c) and the forward edge (a, b->c)
        // both read "a->b->c" when joined with "->".
        const edges = [edge("start", "c"), edge("c", "a->b"), edge("a->b", "c"), edge("start", "a"), edge("a", "b->c")];
        const laid = autoLayout(nodes, edges);
        expect(at(laid, "a").x).toBeLessThan(at(laid, "b->c").x); // the forward edge a → b->c still counts
        expect(at(laid, "c").x).toBeLessThan(at(laid, "a->b").x);
    });

    it("keeps the graph where it was: the layout starts at the old top-left corner", () => {
        const nodes = [
            { ...node("s", "start", SIZE), position: { x: 2000, y: 900 } },
            { ...node("a", "llm", SIZE), position: { x: 1500, y: 1200 } },
        ] as Node[];
        const laid = autoLayout(nodes, [edge("s", "a")]);
        expect(Math.min(...laid.map((n) => n.position.x))).toBe(1500);
        expect(Math.min(...laid.map((n) => n.position.y))).toBe(900);
    });

    it("barycenter ordering keeps a fan-out and its fan-in uncrossed", () => {
        const nodes = ["s", "a", "b", "a2", "b2"].map((id) => node(id, "llm", SIZE));
        // a2 listed before its predecessor's sibling: ordering must follow predecessors, not input order.
        const edges = [edge("s", "a"), edge("s", "b"), edge("b", "b2"), edge("a", "a2")];
        const laid = autoLayout([nodes[0], nodes[1], nodes[2], nodes[4], nodes[3]], edges);
        expect(Math.sign(at(laid, "a").y - at(laid, "b").y)).toBe(Math.sign(at(laid, "a2").y - at(laid, "b2").y));
    });
});

describe("collapsed (the nodes a copilot proposal adds land folded)", () => {
    const expand = (n: Node) => (n.data as { expand?: boolean }).expand;
    it("folds the new nodes and leaves the input untouched", () => {
        const input = [node("a"), { ...node("b"), data: { type: "llm", expand: true } } as Node];
        const out = collapsed(input, new Set());
        expect(out.map(expand)).toEqual([false, false]);
        expect(expand(input[1])).toBe(true);
        expect(out[0].data).not.toBe(input[0].data); // a new data object: the node component sees the change
    });
    it("keeps the nodes already on the canvas as they are (the user's folds stay)", () => {
        const kept = { ...node("old"), data: { type: "llm", expand: true } } as Node;
        const out = collapsed([kept, node("new")], new Set(["old"]));
        expect(out[0]).toBe(kept);
        expect(expand(out[1])).toBe(false);
    });
    it("leaves notes alone (they have no fold)", () => {
        const note = { ...node("n"), type: "noteNode" } as Node;
        expect(collapsed([note], new Set())[0]).toBe(note);
    });
});

describe("sizeSettler (lay out once the new sizes have settled)", () => {
    const small = sizeKey([node("a", "llm", { width: 300, height: 80 })]);
    const big = sizeKey([node("a", "llm", SIZE)]);
    it("sizeKey changes with a measured size", () => {
        expect(small).not.toBe(big);
        expect(sizeKey([node("a", "llm", SIZE)])).toBe(big);
    });
    it("after a fold: waits for the size to change, then for it to hold for a frame", () => {
        const settled = sizeSettler(big);
        expect(settled(big)).toBe(false); // not re-measured yet
        expect(settled(small)).toBe(false); // changed, not yet steady
        expect(settled(small)).toBe(true);
    });
    it("a size that flickers is not settled", () => {
        const settled = sizeSettler(big);
        expect(settled(small)).toBe(false);
        expect(settled(big)).toBe(false);
        expect(settled(small)).toBe(false);
        expect(settled(small)).toBe(true);
    });
});
