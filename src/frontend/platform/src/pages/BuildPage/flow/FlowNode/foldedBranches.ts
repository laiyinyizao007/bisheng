import { createContext } from "react";

/**
 * Folding condition and output nodes. Their outgoing handles live inside the node body
 * (a condition has one per case plus "else"; an output with choose interaction one per
 * option), so a folded body cannot simply be clipped: the lines would lose what they hang
 * on. A folded one shows one compact row per outgoing handle instead, with the same
 * handle id, and the body's own handles step aside while it is folded.
 */

export type FoldedRow =
    | { kind: "if"; handleId: string; operator: string; terms: { left: string; op: string; right: string }[] }
    | { kind: "else"; handleId: "" }
    | { kind: "option"; handleId: string; text: string };

/** True inside a folded node body: the handles there step aside for the compact rows. */
export const BodyFolded = createContext(false);

/** Node types with a fold toggle: all but end, which has no body worth folding. */
export function foldable(type: string): boolean {
    return type !== "end";
}

interface Param {
    type?: string;
    value?: unknown;
    options?: unknown;
}

interface NodeLike {
    type: string;
    group_params?: { params?: Param[] }[];
}

function param(node: NodeLike, type: string): Param | undefined {
    for (const group of node.group_params ?? []) {
        const found = (group.params ?? []).find((p) => p.type === type);
        if (found) return found;
    }
    return undefined;
}

const text = (value: unknown): string => (typeof value === "string" ? value : "");
const record = (value: unknown): Record<string, unknown> => (value && typeof value === "object" ? (value as Record<string, unknown>) : {});

/** The rows a folded node keeps; empty when its only outgoing handle is the default one. */
export function foldedBranches(node: NodeLike): FoldedRow[] {
    if (node.type === "condition") {
        const cases = param(node, "condition")?.value;
        const rows: FoldedRow[] = (Array.isArray(cases) ? cases : []).map((raw) => {
            const item = record(raw);
            const conditions = Array.isArray(item.conditions) ? item.conditions : [];
            return {
                kind: "if" as const,
                handleId: text(item.id),
                operator: text(item.operator) || "and",
                terms: conditions.map((c) => {
                    const term = record(c);
                    return {
                        left: text(term.left_label) || text(term.left_var),
                        op: text(term.comparison_operation),
                        right: text(term.right_label) || text(term.right_value),
                    };
                }),
            };
        });
        return [...rows, { kind: "else", handleId: "" }];
    }
    if (node.type === "output") {
        const form = param(node, "output_form");
        if (record(form?.value).type !== "choose") return [];
        const options = Array.isArray(form?.options) ? form.options : [];
        return options.map((raw) => {
            const option = record(raw);
            return { kind: "option" as const, handleId: text(option.id), text: text(option.label) };
        });
    }
    return [];
}
