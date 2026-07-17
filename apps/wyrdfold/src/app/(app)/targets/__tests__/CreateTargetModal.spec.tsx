import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import CreateTargetModal from '../CreateTargetModal';

// The Search tab (default) fetches from /api/targets/search; stub fetch so the
// modal mounts without a real request. Search behaviour is covered in depth by
// TargetSearchTab.spec.tsx — here we only prove modal composition + create.
const ORIGINAL_FETCH = global.fetch;
beforeEach(() => {
  global.fetch = jest.fn().mockResolvedValue({
    ok: true,
    json: async () => ({ results: [] }),
  }) as unknown as typeof fetch;
});
afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

function renderModal(
  overrides: Partial<React.ComponentProps<typeof CreateTargetModal>> = {}
) {
  const props = {
    isOpen: true,
    onClose: jest.fn(),
    onSubmitManual: jest.fn(),
    onSubmitUrl: jest.fn(),
    onFollow: jest.fn().mockResolvedValue(true),
    onCreateSuggestion: jest.fn().mockResolvedValue(true),
    ...overrides,
  };
  render(<CreateTargetModal {...props} />);
  return props;
}

describe('CreateTargetModal', () => {
  it('renders nothing when isOpen is false', () => {
    renderModal({ isOpen: false });
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('opens on the Search tab with Search / Manual / From URL tabs', () => {
    renderModal();
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /search/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /manual/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /from url/i })).toBeInTheDocument();
    // Discovery-first: the search box shows and the create-submit button is
    // hidden (following a result is inline, not a footer submit).
    expect(
      screen.getByRole('textbox', { name: /search existing targets/i })
    ).toBeInTheDocument();
    expect(
      screen.queryByRole('button', { name: /create target/i })
    ).not.toBeInTheDocument();
  });

  it('disables Create Target until a label is entered (manual mode)', async () => {
    const user = userEvent.setup();
    renderModal();
    await user.click(screen.getByRole('tab', { name: /manual/i }));

    const submit = screen.getByRole('button', { name: /create target/i });
    expect(submit).toBeDisabled();

    await user.type(
      screen.getByRole('textbox', { name: /title/i }),
      'Frontend Engineer'
    );
    await waitFor(() => expect(submit).toBeEnabled());
  });

  it('submits a manual target with trimmed label and optional description', async () => {
    const user = userEvent.setup();
    const { onSubmitManual } = renderModal();
    await user.click(screen.getByRole('tab', { name: /manual/i }));

    await user.type(
      screen.getByRole('textbox', { name: /title/i }),
      '  Frontend Engineer  '
    );
    await user.click(screen.getByRole('button', { name: /create target/i }));

    expect(onSubmitManual).toHaveBeenCalledWith({
      label: 'Frontend Engineer',
      description: undefined,
    });
  });

  it('switches to URL mode and submits with jd_url', async () => {
    const user = userEvent.setup();
    const { onSubmitUrl } = renderModal();

    await user.click(screen.getByRole('tab', { name: /from url/i }));
    await user.type(
      screen.getByRole('textbox', { name: /job description url/i }),
      'https://example.com/jd'
    );
    await user.click(screen.getByRole('button', { name: /create target/i }));

    expect(onSubmitUrl).toHaveBeenCalledWith({
      jd_url: 'https://example.com/jd',
      label: undefined,
    });
  });

  it('calls onClose when Cancel is clicked', async () => {
    const user = userEvent.setup();
    const { onClose } = renderModal();
    await user.click(screen.getByRole('button', { name: /cancel/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it('closes the dialog when Escape is pressed', async () => {
    const user = userEvent.setup();
    const { onClose } = renderModal();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });
});
