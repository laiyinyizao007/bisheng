import { afterEach, expect, it, vi } from 'vitest';
import request from '../request';
import source from './workflow.ts?raw';
import { getWorkflowNodeTemplate } from './workflow';

// F005 T042: node templates are owned by the backend; the editor fetches them and keeps no copy.
vi.mock('../request', () => ({ default: { get: vi.fn(), post: vi.fn() } }));
afterEach(() => vi.mocked(request.get).mockReset());

const templates = [{ type: 'start', group_params: [] }, { type: 'end', group_params: [] }];

it('fetches the node templates from the backend endpoint', async () => {
    vi.mocked(request.get).mockResolvedValueOnce(templates);
    await expect(getWorkflowNodeTemplate()).resolves.toEqual(templates);
    expect(request.get).toHaveBeenCalledWith('/api/v1/workflow/node_templates');
});

it('rejects instead of falling back when the request fails', async () => {
    const failure = new Error('platform down');
    vi.mocked(request.get).mockRejectedValueOnce(failure);
    await expect(getWorkflowNodeTemplate()).rejects.toBe(failure);
});

it.each([
    [[]],
    [null],
    [{ type: 'start' }],
    // Entries the editor cannot build a node from: they would throw later, inside the drop handler.
    [[{ type: 'llm' }]],
    [[{ type: 'llm', group_params: null }]],
    [[{ group_params: [] }]],
    [[{ type: 'start', group_params: [] }, 'llm']],
])('rejects a reply that is not a list of node templates: %j', async (reply) => {
    vi.mocked(request.get).mockResolvedValueOnce(reply);
    const failure = await getWorkflowNodeTemplate().then(() => null, (error: Error) => error);
    expect(failure).toBeInstanceOf(Error);
    // The reason reaches the node panel, so it is the localized generic copy, not developer text.
    expect(failure?.message).not.toMatch(/non-empty|list/);
    // Same default as the shared request layer, so a missing bundle never prints the raw key.
    expect(failure?.message).toBe('Request failed. Please try again.');
});

// A template literal is written as object keys (any quote style); code that reads nodes uses `.group_params`.
const TEMPLATE_KEY = /(?:['"`]group_params['"`]|[{,]\s*group_params)\s*:/;

it('ships no built-in copy of the templates', () => {
    expect(source).not.toContain('workflowTemplate');
    expect(source).not.toMatch(TEMPLATE_KEY);
});

it.each([
    `const t = [{"group_params": []}]`,
    `const t=[{'group_params': [], 'type': 'start'}]`,
    'const t = [{ group_params: [] }]',
])('the copy detector recognizes a template literal: %s', (literal) => {
    expect(literal).toMatch(TEMPLATE_KEY);
});

it('the copy detector ignores code that reads nodes', () => {
    expect('const { version_key } = nodeData.group_params[0].params[0].value').not.toMatch(TEMPLATE_KEY);
});
