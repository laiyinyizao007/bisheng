import { describe, expect, it } from "vitest";
import { foldable, foldedBranches } from "./foldedBranches";

const condition = (cases: unknown[]) => ({
    type: "condition",
    group_params: [{ params: [{ key: "condition", type: "condition", value: cases }] }],
});
const output = (value: unknown, options: unknown[] = []) => ({
    type: "output",
    group_params: [{ params: [{ key: "output_result", type: "output_form", value, options }] }],
});

describe("foldable (which nodes get a fold toggle)", () => {
    it("every node but end; condition and output now too", () => {
        expect(["llm", "input", "condition", "output", "tool"].every(foldable)).toBe(true);
        expect(foldable("end")).toBe(false);
    });
});

describe("foldedBranches (the rows a folded node keeps, one per outgoing handle)", () => {
    it("a condition: one row per case, keyed by the case id, then else on the default handle", () => {
        const rows = foldedBranches(
            condition([
                {
                    id: "c1",
                    operator: "and",
                    conditions: [
                        { left_var: "in.amount", left_label: "amount", comparison_operation: "greater_than_or_equal", right_value: "500", right_label: "" },
                        { left_var: "in.cur", left_label: "", comparison_operation: "equals", right_value: "CNY", right_label: "" },
                    ],
                },
                { id: "c2", operator: "or", conditions: [{ left_var: "in.x", comparison_operation: "is_empty", right_value: "" }] },
            ]),
        );
        expect(rows).toEqual([
            { kind: "if", handleId: "c1", operator: "and", terms: [{ left: "amount", op: "greater_than_or_equal", right: "500" }, { left: "in.cur", op: "equals", right: "CNY" }] },
            { kind: "if", handleId: "c2", operator: "or", terms: [{ left: "in.x", op: "is_empty", right: "" }] },
            { kind: "else", handleId: "" },
        ]);
    });

    it("an output with choose interaction: one row per option, keyed by the option id", () => {
        const rows = foldedBranches(output({ type: "choose" }, [{ id: "o1", label: "Approve" }, { id: "o2", label: "Reject" }]));
        expect(rows).toEqual([
            { kind: "option", handleId: "o1", text: "Approve" },
            { kind: "option", handleId: "o2", text: "Reject" },
        ]);
    });

    it("an output without choose has a single default handle (no rows)", () => {
        expect(foldedBranches(output({ type: "none" }))).toEqual([]);
        expect(foldedBranches(output({ type: "input" }))).toEqual([]);
    });

    it("a case or option without an id gets no row: its handle would fall back to the default one", () => {
        expect(foldedBranches(condition([{ id: "", conditions: [] }, { conditions: [] }, { id: "c1" }]))).toEqual([
            { kind: "if", handleId: "c1", operator: "and", terms: [] },
            { kind: "else", handleId: "" },
        ]);
        expect(foldedBranches(output({ type: "choose" }, [{ label: "no id" }, { id: "o1", label: "ok" }]))).toEqual([
            { kind: "option", handleId: "o1", text: "ok" },
        ]);
    });

    it("tolerates missing or malformed parameters", () => {
        expect(foldedBranches({ type: "condition", group_params: [] })).toEqual([{ kind: "else", handleId: "" }]);
        expect(foldedBranches(condition([{ id: "c1" }]))).toEqual([{ kind: "if", handleId: "c1", operator: "and", terms: [] }, { kind: "else", handleId: "" }]);
        expect(foldedBranches(output(undefined))).toEqual([]);
        expect(foldedBranches({ type: "llm", group_params: [] })).toEqual([]);
    });
});
