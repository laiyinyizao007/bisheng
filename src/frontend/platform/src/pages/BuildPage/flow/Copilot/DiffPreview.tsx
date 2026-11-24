import type { CopilotNeed, CopilotProposal } from "@/controllers/API/workflowCopilot";
import { useTranslation } from "react-i18next";

/**
 * F007 AC-17: the proposal as a diff against the canvas that was sent — the same diff
 * the backend recomputed from the merged canvas, so what is listed is what "apply" lands.
 * Unchanged content is not listed. Resources left for the user are marked (AC-15).
 */

type Translate = (key: string, options?: Record<string, unknown>) => string;

/** `t()` with a readable fallback: an uninitialized i18next returns undefined or the key itself. */
export function label(t: Translate, key: string, fallback: string, options?: Record<string, unknown>): string {
    const text = t(key, options);
    return typeof text === "string" && text && text !== key ? text : fallback;
}

function nodeTitle(t: Translate, node: { id: string; type?: string | null; name?: string | null }): string {
    const kind = node.type ? label(t, `node.${node.type}.name`, node.type) : "";
    const name = node.name || node.id;
    return kind && kind !== name ? `${name} (${kind})` : name;
}

export function needText(t: Translate, need: CopilotNeed, nodeNames: Record<string, string>): string {
    const node = nodeNames[need.node_id] ?? need.node_id;
    if (need.kind === "tool" && need.param_key === "tool") {
        return label(t, "copilot.needsTool", `${node}: ${need.wanted}`, { node, wanted: need.wanted });
    }
    return label(t, "copilot.needsItem", `${node} / ${need.param_key}: ${need.wanted}`, {
        node,
        param: need.param_key,
        wanted: need.wanted,
    });
}

interface Props {
    proposal: CopilotProposal;
}

export default function DiffPreview({ proposal }: Props) {
    const { t } = useTranslation("flow");
    const tr = t as unknown as Translate;
    const { diff, needs_selection: needs, replaced } = proposal;
    const types: Record<string, string> = {};
    const names: Record<string, string> = {};
    for (const node of proposal.canvas.nodes as { id: string; data?: { type?: string; name?: string } }[]) {
        if (node.data?.type) types[node.id] = node.data.type;
        names[node.id] = node.data?.name || node.id;
    }
    const empty =
        !diff.nodes_only_local.length &&
        !diff.nodes_only_online.length &&
        !diff.nodes_changed.length &&
        !diff.edges_only_local.length &&
        !diff.edges_only_online.length;

    return (
        <div className="mt-2 rounded-lg border p-3 text-sm" data-testid="copilot-diff">
            <div className="mb-2 font-medium">{label(tr, "copilot.diffTitle", "Proposed changes")}</div>
            {empty && <div className="text-gray-500">{label(tr, "copilot.diffNone", "No changes")}</div>}
            <Section title={label(tr, "copilot.diffAdded", "Added nodes")} items={diff.nodes_only_local.map((n) => nodeTitle(tr, n))} tone="add" />
            <Section title={label(tr, "copilot.diffRemoved", "Removed nodes")} items={diff.nodes_only_online.map((n) => nodeTitle(tr, n))} tone="remove" />
            {diff.nodes_changed.length > 0 && (
                <div className="mb-2">
                    <div className="text-gray-600">{label(tr, "copilot.diffChanged", "Changed nodes")}</div>
                    <ul className="ml-4 list-disc">
                        {diff.nodes_changed.map((change) => {
                            const type = types[change.id];
                            const keys = [
                                ...change.fields.map((f) => f.field),
                                ...change.params.map((p) => (type ? label(tr, `node.${type}.${p.key}.label`, p.key) : p.key)),
                            ];
                            const whole = replaced.includes(change.id) ? ` · ${label(tr, "copilot.diffReplaced", "replaced")}` : "";
                            return (
                                <li key={change.id}>
                                    {nodeTitle(tr, { id: change.id, type, name: names[change.id] })}: {keys.join(", ")}
                                    {whole}
                                </li>
                            );
                        })}
                    </ul>
                </div>
            )}
            <Section
                title={label(tr, "copilot.diffEdgesAdded", "Added connections")}
                items={diff.edges_only_local.map((e) => `${names[e.source] ?? e.source} → ${names[e.target] ?? e.target}`)}
                tone="add"
            />
            <Section
                title={label(tr, "copilot.diffEdgesRemoved", "Removed connections")}
                items={diff.edges_only_online.map((e) => `${names[e.source] ?? e.source} → ${names[e.target] ?? e.target}`)}
                tone="remove"
            />
            {needs.length > 0 && (
                <div className="mt-2 rounded-md bg-amber-50 p-2 dark:bg-amber-950" data-testid="copilot-needs">
                    <div className="font-medium text-amber-700 dark:text-amber-300">{label(tr, "copilot.needsTitle", "Needs your choice")}</div>
                    <ul className="ml-4 list-disc">
                        {needs.map((need) => (
                            <li key={`${need.node_id}:${need.param_key}:${need.wanted}`}>
                                {needText(tr, need, names)}
                                {need.candidates.length > 0 && (
                                    <span className="text-gray-500">
                                        {" "}
                                        {label(tr, "copilot.needsCandidates", need.candidates.join(", "), { list: need.candidates.join(", ") })}
                                    </span>
                                )}
                            </li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}

function Section({ title, items, tone }: { title: string; items: string[]; tone: "add" | "remove" }) {
    if (!items.length) return null;
    return (
        <div className="mb-2">
            <div className="text-gray-600">{title}</div>
            <ul className={`ml-4 list-disc ${tone === "add" ? "text-green-700 dark:text-green-400" : "text-red-700 dark:text-red-400 line-through"}`}>
                {items.map((item, index) => (
                    <li key={`${item}-${index}`}>{item}</li>
                ))}
            </ul>
        </div>
    );
}
