import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import DataExportCard from '../DataExportCard';

const mockToast = jest.fn();

jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

// jsdom implements neither URL.createObjectURL nor anchor-driven
// navigation, so stub the blob→download plumbing the component relies on.
const mockCreateObjectURL = jest.fn(() => 'blob:mock-url');
const mockRevokeObjectURL = jest.fn();
const mockAnchorClick = jest.fn();

beforeEach(() => {
  mockToast.mockClear();
  mockCreateObjectURL.mockClear();
  mockRevokeObjectURL.mockClear();
  mockAnchorClick.mockClear();
  global.fetch = jest.fn() as jest.Mock;
  global.URL.createObjectURL = mockCreateObjectURL;
  global.URL.revokeObjectURL = mockRevokeObjectURL;
  jest
    .spyOn(HTMLAnchorElement.prototype, 'click')
    .mockImplementation(mockAnchorClick);
});

afterEach(() => {
  jest.restoreAllMocks();
});

describe('DataExportCard', () => {
  it('renders the heading + download button', () => {
    render(<DataExportCard />);
    expect(
      screen.getByText(/Download a ZIP of everything/i)
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Download my data/i })
    ).toBeInTheDocument();
  });

  it('fetches the export and triggers a blob download on success', async () => {
    const blob = new Blob(['zip-bytes'], { type: 'application/zip' });
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: true,
      status: 200,
      blob: async () => blob,
    });
    const user = userEvent.setup();
    render(<DataExportCard />);

    await user.click(screen.getByRole('button', { name: /Download my data/i }));

    await waitFor(() =>
      expect(global.fetch).toHaveBeenCalledWith('/api/profile/export')
    );
    expect(mockCreateObjectURL).toHaveBeenCalledWith(blob);
    expect(mockAnchorClick).toHaveBeenCalledTimes(1);
    expect(mockRevokeObjectURL).toHaveBeenCalledWith('blob:mock-url');
    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'success' })
      )
    );
  });

  it('toasts the upstream error and skips the download when the request fails', async () => {
    (global.fetch as jest.Mock).mockResolvedValueOnce({
      ok: false,
      status: 401,
      // extractApiError reads the body via res.clone().json().
      clone: () => ({ json: async () => ({ detail: 'Unauthorized' }) }),
      json: async () => ({ detail: 'Unauthorized' }),
    });
    const user = userEvent.setup();
    render(<DataExportCard />);

    await user.click(screen.getByRole('button', { name: /Download my data/i }));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error', title: 'Unauthorized' })
      )
    );
    expect(mockCreateObjectURL).not.toHaveBeenCalled();
    expect(mockAnchorClick).not.toHaveBeenCalled();
  });

  it('toasts a network error when the fetch rejects', async () => {
    (global.fetch as jest.Mock).mockRejectedValueOnce(new Error('boom'));
    const user = userEvent.setup();
    render(<DataExportCard />);

    await user.click(screen.getByRole('button', { name: /Download my data/i }));

    await waitFor(() =>
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      )
    );
    expect(mockAnchorClick).not.toHaveBeenCalled();
  });
});
