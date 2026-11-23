import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { getToolsApi } from '@/controllers/API/tools';
import { getWorkflowNodeTemplate } from '@/controllers/API/workflow';
import Sidebar from './Sidebar';

// F005 T042/T043: with node and tool templates owned by the backend, a failed or partial fetch must
// read as a failure in the node panel — never as an empty or silently shorter list, never as a local copy.
const stableTranslation = vi.hoisted(() => (key: string, options?: Record<string, string>) =>
    options ? `${key}:${Object.values(options).join('|')}` : key);
vi.mock('react-i18next', () => ({ useTranslation: () => ({ t: stableTranslation }) }));
vi.mock('@/controllers/API/workflow', () => ({ getWorkflowNodeTemplate: vi.fn() }));
vi.mock('@/controllers/API/tools', () => ({ getToolsApi: vi.fn() }));
afterEach(() => {
    cleanup();
    vi.mocked(getWorkflowNodeTemplate).mockReset();
    vi.mocked(getToolsApi).mockReset();
});

const templates = [{ type: 'start', group_params: [] }, { type: 'llm', group_params: [] }];

function renderSidebar() {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(<QueryClientProvider client={client}><Sidebar /></QueryClientProvider>);
}

it('lists the node types the backend returns', async () => {
    vi.mocked(getWorkflowNodeTemplate).mockResolvedValue(templates);
    vi.mocked(getToolsApi).mockResolvedValue([]);
    renderSidebar();
    expect(await screen.findByText('node.llm.name')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('says the nodes failed to load, with the reason, and retries on request', async () => {
    vi.mocked(getWorkflowNodeTemplate).mockRejectedValueOnce(new Error('HTTP 500')).mockResolvedValue(templates);
    vi.mocked(getToolsApi).mockResolvedValue([]);
    renderSidebar();
    expect(await screen.findByRole('alert')).toHaveTextContent('nodeTemplatesLoadFailed:HTTP 500');
    expect(screen.queryByText('node.llm.name')).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'retry' }));
    expect(await screen.findByText('node.llm.name')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('names the tools the backend sent without a node template', async () => {
    vi.mocked(getWorkflowNodeTemplate).mockResolvedValue(templates);
    vi.mocked(getToolsApi).mockResolvedValue([
        { id: 3, name: 'web', is_preset: 0, children: [{ id: 1, name: 'search', tool_key: 'k1', api_params: [] }] },
    ]);
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    renderSidebar();
    fireEvent.mouseDown(screen.getByRole('tab', { name: 'toolNodes' }), { button: 0, ctrlKey: false });
    expect(await screen.findByRole('alert')).toHaveTextContent('toolTemplatesUnavailable:search');
});
