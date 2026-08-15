import React from 'react';
import '@testing-library/jest-dom';
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import ReferenceJDList from '../ReferenceJDList';
import type { TargetReferenceJD } from '../../types';

const mockToast = jest.fn();
jest.mock('@/state/Toast/ToastProvider', () => ({
  useToast: () => ({ toast: mockToast }),
}));

// AddReferenceJDModal renders nothing when closed; replace with a stub to keep
// the spec focused on the list itself and avoid pulling in modal internals.
jest.mock('../AddReferenceJDModal', () => ({
  __esModule: true,
  default: () => null,
}));

const SAMPLE_JD: TargetReferenceJD = {
  id: 'jd-1',
  target_id: 't-1',
  jd_url: 'https://example.com/jd',
  jd_text: 'A long job description text snippet for the role.',
  extracted_profile: {
    categories: {},
    seniority: { level: null, signals: [] },
    domain: { signals: [], weight: 0.5 },
    negative: { keywords: [], weight: -10 },
  },
  created_at: '2026-04-30T00:00:00Z',
};

const originalFetch = global.fetch;

beforeEach(() => {
  mockToast.mockReset();
  global.fetch = jest.fn() as unknown as typeof fetch;
});

afterEach(() => {
  global.fetch = originalFetch;
});

describe('ReferenceJDList', () => {
  it('shows an empty state when no reference JDs exist', () => {
    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[]}
        onChanged={() => undefined}
      />
    );
    expect(screen.getByText(/No reference JDs yet/i)).toBeInTheDocument();
  });

  it('renders one row per reference JD with the count in the title', () => {
    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[SAMPLE_JD, { ...SAMPLE_JD, id: 'jd-2' }]}
        onChanged={() => undefined}
      />
    );
    expect(screen.getByText(/Reference JDs \(2\)/i)).toBeInTheDocument();
    expect(screen.getAllByLabelText('Delete reference JD')).toHaveLength(2);
  });

  it('asks for confirmation before deleting and aborts when cancelled', async () => {
    const onChanged = jest.fn();
    const user = userEvent.setup();

    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[SAMPLE_JD]}
        onChanged={onChanged}
      />
    );
    // The trash button only opens the confirm dialog.
    await user.click(screen.getByLabelText('Delete reference JD'));
    const dialog = await screen.findByRole('dialog');
    expect(global.fetch).not.toHaveBeenCalled();

    // Cancelling closes the dialog without deleting.
    await user.click(within(dialog).getByRole('button', { name: /cancel/i }));
    expect(global.fetch).not.toHaveBeenCalled();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('deletes the JD and notifies onChanged when confirmed', async () => {
    const onChanged = jest.fn();
    (global.fetch as jest.Mock).mockResolvedValue({ ok: true } as Response);
    const user = userEvent.setup();

    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[SAMPLE_JD]}
        onChanged={onChanged}
      />
    );
    await user.click(screen.getByLabelText('Delete reference JD'));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /^delete$/i }));

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/targets/t-1/reference-jds/jd-1',
        { method: 'DELETE' }
      );
      expect(onChanged).toHaveBeenCalled();
    });
    expect(mockToast).toHaveBeenCalledWith(
      expect.objectContaining({ variant: 'success' })
    );
  });

  it('toasts an error when delete fails', async () => {
    const onChanged = jest.fn();
    (global.fetch as jest.Mock).mockResolvedValue({ ok: false } as Response);
    const user = userEvent.setup();

    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[SAMPLE_JD]}
        onChanged={onChanged}
      />
    );
    await user.click(screen.getByLabelText('Delete reference JD'));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: /^delete$/i }));

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      );
    });
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('renders up/down vote controls for each reference JD', () => {
    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[SAMPLE_JD, { ...SAMPLE_JD, id: 'jd-2' }]}
        onChanged={() => undefined}
      />
    );
    expect(screen.getAllByLabelText('Upvote reference JD')).toHaveLength(2);
    expect(screen.getAllByLabelText('Downvote reference JD')).toHaveLength(2);
  });

  it('posts an upvote and reflects the server-echoed vote', async () => {
    const onChanged = jest.fn();
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      json: async () => ({ your_vote: 1, profile_version: null }),
    } as Response);
    const user = userEvent.setup();

    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[SAMPLE_JD]}
        onChanged={onChanged}
      />
    );
    const upvote = screen.getByLabelText('Upvote reference JD');
    await user.click(upvote);

    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith(
        '/api/targets/t-1/reference-jds/jd-1/vote',
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ value: 1 }),
        }
      );
    });
    // The button reflects the recorded vote (aria-pressed) once the response
    // lands; suppression didn't flip (profile_version null) so no refetch.
    await waitFor(() => expect(upvote).toHaveAttribute('aria-pressed', 'true'));
    expect(onChanged).not.toHaveBeenCalled();
  });

  it('clears the vote by re-clicking the active direction (value 0)', async () => {
    (global.fetch as jest.Mock)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ your_vote: 1, profile_version: null }),
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ your_vote: 0, profile_version: null }),
      } as Response);
    const user = userEvent.setup();

    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[SAMPLE_JD]}
        onChanged={() => undefined}
      />
    );
    const upvote = screen.getByLabelText('Upvote reference JD');
    await user.click(upvote);
    await waitFor(() => expect(upvote).toHaveAttribute('aria-pressed', 'true'));

    // Second click on the now-active upvote sends value 0 to clear it.
    await user.click(upvote);
    await waitFor(() => {
      expect(global.fetch).toHaveBeenLastCalledWith(
        '/api/targets/t-1/reference-jds/jd-1/vote',
        expect.objectContaining({ body: JSON.stringify({ value: 0 }) })
      );
    });
    await waitFor(() =>
      expect(upvote).toHaveAttribute('aria-pressed', 'false')
    );
  });

  it('refetches when a vote flips suppression (profile re-merged)', async () => {
    const onChanged = jest.fn();
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      json: async () => ({ your_vote: -1, profile_version: 7 }),
    } as Response);
    const user = userEvent.setup();

    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[SAMPLE_JD]}
        onChanged={onChanged}
      />
    );
    await user.click(screen.getByLabelText('Downvote reference JD'));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it('toasts an error when a vote fails and leaves the vote unset', async () => {
    const onChanged = jest.fn();
    (global.fetch as jest.Mock).mockResolvedValue({ ok: false } as Response);
    const user = userEvent.setup();

    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[SAMPLE_JD]}
        onChanged={onChanged}
      />
    );
    const upvote = screen.getByLabelText('Upvote reference JD');
    await user.click(upvote);

    await waitFor(() => {
      expect(mockToast).toHaveBeenCalledWith(
        expect.objectContaining({ variant: 'error' })
      );
    });
    expect(upvote).toHaveAttribute('aria-pressed', 'false');
    expect(onChanged).not.toHaveBeenCalled();
  });
});

/**
 * Affordance gaps found in the second /targets sweep (resweep B5/B6). The
 * rows carried three unlabelled-looking icon buttons with no explanation of
 * what a vote does, a delete dialog that never said WHICH JD, and text
 * clamped to two lines with no way to read the rest — which for a JD pasted
 * without a source URL meant it could not be read back at all.
 */
describe('ReferenceJDList — affordances', () => {
  it('explains what voting does before asking the user to vote', () => {
    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[SAMPLE_JD]}
        onChanged={() => undefined}
      />
    );
    expect(
      screen.getByText(/stops contributing to the shared model/i)
    ).toBeInTheDocument();
  });

  it('names the JD in the delete dialog using its source URL', async () => {
    const user = userEvent.setup();
    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[
          {
            ...SAMPLE_JD,
            jd_url: 'https://boards.example.com/jobs/senior-platform-engineer',
          },
        ]}
        onChanged={() => undefined}
      />
    );
    await user.click(screen.getByLabelText('Delete reference JD'));
    expect(screen.getByText(/senior platform engineer/i)).toBeInTheDocument();
  });

  it('names a URL-less pasted JD by its opening words', async () => {
    const user = userEvent.setup();
    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[
          {
            ...SAMPLE_JD,
            jd_url: null,
            jd_text:
              'Staff Backend Engineer, Clinical Data. Join a healthtech company.',
          },
        ]}
        onChanged={() => undefined}
      />
    );
    await user.click(screen.getByLabelText('Delete reference JD'));
    // Scope to the dialog heading — the same words also sit in the row behind it.
    expect(
      screen.getByRole('heading', { name: /Delete “Staff Backend Engineer/i })
    ).toBeInTheDocument();
  });

  it('distinguishes the two rows when there is more than one JD', async () => {
    const user = userEvent.setup();
    render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[
          {
            ...SAMPLE_JD,
            id: 'jd-1',
            jd_url: 'https://x.test/jobs/alpha-role',
          },
          { ...SAMPLE_JD, id: 'jd-2', jd_url: 'https://x.test/jobs/beta-role' },
        ]}
        onChanged={() => undefined}
      />
    );
    // Deleting the SECOND row must name the second role, not the first.
    const [, secondDelete] = screen.getAllByLabelText('Delete reference JD');
    expect(secondDelete).toBeDefined();
    await user.click(secondDelete as HTMLElement);
    expect(screen.getByText(/beta role/i)).toBeInTheDocument();
    expect(screen.queryByText(/alpha role/i)).not.toBeInTheDocument();
  });

  it('expands a clamped JD and collapses it again', async () => {
    const user = userEvent.setup();
    const { container } = render(
      <ReferenceJDList
        targetId='t-1'
        referenceJDs={[SAMPLE_JD]}
        onChanged={() => undefined}
      />
    );
    const clamped = () => container.querySelector('.line-clamp-2');
    expect(clamped()).not.toBeNull();

    await user.click(screen.getByRole('button', { name: /show full jd/i }));
    expect(clamped()).toBeNull();

    await user.click(screen.getByRole('button', { name: /show less/i }));
    expect(clamped()).not.toBeNull();
  });
});
