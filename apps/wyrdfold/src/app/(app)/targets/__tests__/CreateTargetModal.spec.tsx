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

  it('calls onClose when the dismiss button is clicked', async () => {
    const user = userEvent.setup();
    const { onClose } = renderModal();
    await user.click(screen.getByRole('button', { name: /done/i }));
    expect(onClose).toHaveBeenCalled();
  });

  it('closes the dialog when Escape is pressed', async () => {
    const user = userEvent.setup();
    const { onClose } = renderModal();
    await user.keyboard('{Escape}');
    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  /**
   * Following a target from the Search tab applies immediately — there is no
   * draft to abandon — so a footer reading "Cancel" told the user their
   * just-completed follow was about to be undone.
   */
  describe('dismiss button wording', () => {
    it('reads "Done" on Search, where actions are already applied', () => {
      renderModal();
      expect(screen.getByRole('button', { name: /done/i })).toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: /cancel/i })
      ).not.toBeInTheDocument();
    });

    it('reads "Cancel" on Manual, where there is an unsaved draft', async () => {
      const user = userEvent.setup();
      renderModal();
      await user.click(screen.getByRole('tab', { name: /manual/i }));
      expect(
        screen.getByRole('button', { name: /cancel/i })
      ).toBeInTheDocument();
      expect(
        screen.queryByRole('button', { name: /done/i })
      ).not.toBeInTheDocument();
    });
  });

  /**
   * A failed create was reported ONLY by a toast, which auto-dismisses, while
   * this modal re-opened holding the draft and showing nothing. The from-url
   * fetch runs 10-20s, so the user who looks away comes back to a modal that
   * looks untouched.
   */
  describe('durable failure reason', () => {
    const REASON = 'Could not extract a job description from that URL.';

    it('shows the reason on the tab the failure came from', () => {
      renderModal({
        draft: { mode: 'url', jdUrl: 'https://example.com/not-a-job' },
        error: REASON,
      });
      expect(screen.getByRole('alert')).toHaveTextContent(REASON);
      // ...alongside the draft it failed with, so the URL can be edited.
      expect(
        screen.getByDisplayValue('https://example.com/not-a-job')
      ).toBeInTheDocument();
    });

    it('hides the reason once the user moves to another tab', async () => {
      const user = userEvent.setup();
      renderModal({
        draft: { mode: 'url', jdUrl: 'https://example.com/not-a-job' },
        error: REASON,
      });
      expect(screen.getByRole('alert')).toBeInTheDocument();
      await user.click(screen.getByRole('tab', { name: /manual/i }));
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });

    it('shows no alert when there is no error', () => {
      renderModal();
      expect(screen.queryByRole('alert')).not.toBeInTheDocument();
    });
  });

  /**
   * The zero-result search used to offer only the LLM fallback, so a user who
   * knew exactly what they wanted had to switch tabs and retype it.
   */
  it('carries a dead-end search query into the Manual tab', async () => {
    const user = userEvent.setup();
    renderModal();

    await user.type(
      screen.getByRole('textbox', { name: /search existing targets/i }),
      'data engineer'
    );
    const createManually = await screen.findByRole(
      'button',
      { name: /create .*data engineer.* manually/i },
      { timeout: 3000 }
    );
    await user.click(createManually);

    // Landed on Manual with the query already in the Title field.
    expect(screen.getByRole('textbox', { name: /^title$/i })).toHaveValue(
      'data engineer'
    );
    expect(
      screen.getByRole('button', { name: /create target/i })
    ).toBeEnabled();
  });
});
