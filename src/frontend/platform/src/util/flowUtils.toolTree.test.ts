import { afterEach, expect, it, vi } from 'vitest';
import { getToolTree } from './flowUtils';

// F005 T043: a tool node's template is built by the backend (each child's `node_template`);
// the editor passes it through and keeps the tree shape {name, is_preset, children}.
afterEach(() => vi.restoreAllMocks());

const nodeTemplate = (id: number, key: string) => ({
    id,
    tool_key: key,
    type: 'tool',
    name: key,
    description: `${key} desc`,
    group_params: [{ name: 'backend-group', params: [{ key: 'q', type: 'var_textarea', value: '' }] }],
});

it('uses the backend node template of every child, unchanged', () => {
    const first = nodeTemplate(1, 'search');
    const second = nodeTemplate(2, 'fetch');
    const tree = getToolTree({
        name: 'web',
        is_preset: 0,
        children: [
            { id: 1, name: 'search', api_params: [{ name: 'ignored' }], node_template: first },
            { id: 2, name: 'fetch', api_params: [], node_template: second },
        ],
    });
    expect(tree).toEqual({ name: 'web', is_preset: 0, children: [first, second], unavailable: [] });
    expect(tree.children[0]).toBe(first);
});

it('leaves out a child without a backend template instead of inventing one', () => {
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const kept = nodeTemplate(2, 'fetch');
    const tree = getToolTree({
        name: 'web',
        is_preset: 0,
        children: [
            { id: 1, name: 'search', api_params: [{ name: 'q' }] },
            { id: 2, name: 'fetch', api_params: [], node_template: kept },
        ],
    });
    expect(tree.children).toEqual([kept]);
    // Named so the tool panel can say which tools are missing, not only the console.
    expect(tree.unavailable).toEqual(['search']);
    expect(error).toHaveBeenCalledOnce();
});
