/**
 * F007 AC-19 / design decision 9: did the canvas change between "send" and "apply"?
 *
 * Only what the user edits counts: node id, data and position; edge ends and handles.
 * React Flow's runtime state (`selected`, `dragging`, `measured`, width/height, …) is
 * left out, otherwise clicking a node would look like an edit.
 */

interface DigestNode {
    id: string;
    data?: unknown;
    position?: { x: number; y: number };
}

interface DigestEdge {
    source: string;
    sourceHandle?: string | null;
    target: string;
    targetHandle?: string | null;
}

export function canvasDigest(nodes: readonly DigestNode[], edges: readonly DigestEdge[]): string {
    const shownNodes = nodes
        .map((node) => ({ id: node.id, data: node.data ?? null, position: node.position ? { x: node.position.x, y: node.position.y } : null }))
        .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
    const shownEdges = edges
        .map((edge) => [edge.source, edge.sourceHandle ?? null, edge.target, edge.targetHandle ?? null])
        .sort((a, b) => {
            const left = JSON.stringify(a);
            const right = JSON.stringify(b);
            return left < right ? -1 : left > right ? 1 : 0;
        });
    return stableStringify({ nodes: shownNodes, edges: shownEdges });
}

/** JSON with object keys sorted at every depth, so key order never reads as a change. */
export function stableStringify(value: unknown): string {
    if (Array.isArray(value)) return `[${value.map(stableStringify).join(",")}]`;
    if (value && typeof value === "object") {
        const record = value as Record<string, unknown>;
        return `{${Object.keys(record)
            .filter((key) => record[key] !== undefined)
            .sort()
            .map((key) => `${JSON.stringify(key)}:${stableStringify(record[key])}`)
            .join(",")}}`;
    }
    return JSON.stringify(value ?? null);
}
