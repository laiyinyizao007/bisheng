import type { Edge, Node } from "@xyflow/react";

/**
 * Layered auto layout for the workflow canvas, after the arksusdemo DAG editor's
 * `dagLayout.ts` (hand-written, no layout library):
 *
 * 1. back edges (loops, e.g. output → input for another round) are found by a DFS from
 *    the start node and set aside, so a loop does not drag its target to the right;
 * 2. each node's layer is its longest path from a source;
 * 3. three down/up barycenter sweeps order each layer after its neighbours, so fan-outs
 *    and fan-ins do not cross;
 * 4. placement: left to right (the canvas wires right handle → left handle). Each layer
 *    is a column that starts past the widest node of the column before it, and its
 *    nodes are stacked and centred on a common line. arksusdemo packs each node against
 *    its own predecessors instead; across columns that can overlap a wider sibling, so
 *    whole columns are used here.
 *
 * Sizes are React Flow's measured ones when the node has been rendered, an estimate
 * otherwise; a size that is not a finite positive number falls back to the estimate.
 * The result is anchored at the old top-left corner of the laid-out nodes. Notes keep
 * their place; every other field of every node is kept.
 */

const COL_GAP = 120;
const ROW_GAP = 60;
const ORIGIN = { x: 60, y: 60 };
const NOTE_TYPE = "noteNode";

interface Size {
    width: number;
    height: number;
}

function estimate(node: Node): Size {
    const kind = (node.data as { type?: string } | undefined)?.type;
    if (kind === "start" || kind === "end") return { width: 320, height: 160 };
    const groups = (node.data as { group_params?: { params?: unknown[] }[] } | undefined)?.group_params ?? [];
    const params = groups.reduce((count, group) => count + (group.params?.length ?? 0), 0);
    return { width: 340, height: 120 + 40 * Math.min(params, 10) };
}

/** The first finite, positive candidate: a broken measurement must not poison later columns. */
function usable(...candidates: (number | undefined)[]): number {
    return candidates.find((value): value is number => typeof value === "number" && Number.isFinite(value) && value > 0)!;
}

export function nodeSize(node: Node): Size {
    const guess = estimate(node);
    return {
        width: usable(node.measured?.width, node.width, guess.width),
        height: usable(node.measured?.height, node.height, guess.height),
    };
}

export function autoLayout<N extends Node>(nodes: N[], edges: Edge[]): N[] {
    const placed = nodes.filter((node) => node.type !== NOTE_TYPE);
    if (!placed.length) return nodes;
    const ids = new Set(placed.map((node) => node.id));
    const links = edges.filter((e) => ids.has(e.source) && ids.has(e.target) && e.source !== e.target);

    // 1. Back edges, by a DFS that starts at the start node(s) so the loop-closing edge is the one set aside.
    //    Kept as the edge objects themselves: a joined "source->target" key is ambiguous once ids contain "->".
    const out = new Map<string, Edge[]>(placed.map((node) => [node.id, []]));
    links.forEach((e) => out.get(e.source)!.push(e));
    const roots = [...placed].sort((a, b) => Number(isStart(b)) - Number(isStart(a)));
    const state = new Map<string, 1 | 2>();
    const back = new Set<Edge>();
    for (const root of roots) {
        if (state.has(root.id)) continue;
        // Iterative DFS: a canvas may hold hundreds of nodes.
        const stack: { id: string; next: number }[] = [{ id: root.id, next: 0 }];
        state.set(root.id, 1);
        while (stack.length) {
            const top = stack[stack.length - 1];
            const targets = out.get(top.id)!;
            if (top.next < targets.length) {
                const link = targets[top.next++];
                const target = link.target;
                const seen = state.get(target);
                if (seen === 1) back.add(link);
                else if (seen === undefined) {
                    state.set(target, 1);
                    stack.push({ id: target, next: 0 });
                }
            } else {
                state.set(top.id, 2);
                stack.pop();
            }
        }
    }
    const forward = links.filter((e) => !back.has(e));
    const preds = new Map<string, string[]>(placed.map((node) => [node.id, []]));
    const succs = new Map<string, string[]>(placed.map((node) => [node.id, []]));
    forward.forEach((e) => {
        preds.get(e.target)!.push(e.source);
        succs.get(e.source)!.push(e.target);
    });

    // 2. Longest-path layers, in topological order (Kahn) over the forward edges.
    const layerOf = new Map<string, number>();
    const waiting = new Map(placed.map((node) => [node.id, preds.get(node.id)!.length]));
    const queue = placed.filter((node) => waiting.get(node.id) === 0).map((node) => node.id);
    queue.forEach((id) => layerOf.set(id, 0));
    for (let head = 0; head < queue.length; head++) {
        const id = queue[head];
        for (const next of succs.get(id)!) {
            layerOf.set(next, Math.max(layerOf.get(next) ?? 0, layerOf.get(id)! + 1));
            waiting.set(next, waiting.get(next)! - 1);
            if (waiting.get(next) === 0) queue.push(next);
        }
    }
    placed.forEach((node) => layerOf.has(node.id) || layerOf.set(node.id, 0)); // residual cycles, if any
    const layers: string[][] = [];
    placed.forEach((node) => {
        const layer = layerOf.get(node.id)!;
        while (layers.length <= layer) layers.push([]);
        layers[layer].push(node.id);
    });

    // 3. Barycenter sweeps.
    const order = new Map<string, number>();
    layers.forEach((layer) => layer.forEach((id, index) => order.set(id, index)));
    const center = (id: string, neighbours: Map<string, string[]>) => {
        const around = neighbours.get(id)!;
        return around.length ? around.reduce((sum, n) => sum + order.get(n)!, 0) / around.length : order.get(id)!;
    };
    const sweep = (layer: string[], neighbours: Map<string, string[]>) => {
        const keyed = layer.map((id) => ({ id, key: center(id, neighbours), was: order.get(id)! }));
        keyed.sort((a, b) => a.key - b.key || a.was - b.was);
        keyed.forEach(({ id }, index) => {
            layer[index] = id;
            order.set(id, index);
        });
    };
    for (let round = 0; round < 3; round++) {
        for (let l = 1; l < layers.length; l++) sweep(layers[l], preds);
        for (let l = layers.length - 2; l >= 0; l--) sweep(layers[l], succs);
    }

    // 4. Columns left to right, each layer stacked and centred on one line.
    const byId = new Map(placed.map((node) => [node.id, node]));
    const sizes = new Map(placed.map((node) => [node.id, nodeSize(node)]));
    const columnHeight = (layer: string[]) => layer.reduce((sum, id) => sum + sizes.get(id)!.height, 0) + ROW_GAP * (layer.length - 1);
    // Anchored at the old top-left corner, so the graph stays where the user had it (and
    // near the notes around it) instead of jumping to the canvas origin.
    const origin = corner(placed);
    const middle = origin.y + Math.max(...layers.map(columnHeight)) / 2;
    const positions = new Map<string, { x: number; y: number }>();
    let x = origin.x;
    for (const layer of layers) {
        let y = middle - columnHeight(layer) / 2;
        for (const id of layer) {
            positions.set(id, { x, y });
            y += sizes.get(id)!.height + ROW_GAP;
        }
        x += Math.max(...layer.map((id) => sizes.get(id)!.width)) + COL_GAP;
    }

    return nodes.map((node) => (byId.has(node.id) ? { ...node, position: positions.get(node.id)! } : node));
}

/**
 * Whether React Flow now holds the applied canvas, measured: every applied node (notes
 * aside) is present and has a width. Right after an apply it may still return the old,
 * already-measured canvas, which an "every node is measured" check would wrongly accept.
 */
export function readyToLayout(current: Node[], applied: Node[]): boolean {
    const shown = new Map(current.map((node) => [node.id, node]));
    return applied.every((node) => node.type === NOTE_TYPE || Boolean(shown.get(node.id)?.measured?.width));
}

function corner(nodes: Node[]): { x: number; y: number } {
    const xs = nodes.map((node) => node.position?.x).filter((v): v is number => Number.isFinite(v));
    const ys = nodes.map((node) => node.position?.y).filter((v): v is number => Number.isFinite(v));
    return { x: xs.length ? Math.min(...xs) : ORIGIN.x, y: ys.length ? Math.min(...ys) : ORIGIN.y };
}

function isStart(node: Node): boolean {
    return (node.data as { type?: string } | undefined)?.type === "start";
}
