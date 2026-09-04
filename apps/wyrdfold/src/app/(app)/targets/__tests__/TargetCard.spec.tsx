import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import TargetCard from '../TargetCard';
import type { JobTargetSummary } from '../types';

const mockPush = jest.fn();
jest.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush }),
}));

function makeTarget(
  overrides: Partial<JobTargetSummary> = {}
): JobTargetSummary {
  return {
    id: 't-1',
    label: 'Senior Frontend Engineer',
    description: null,
    normalized_label: null,
    activation_status: 'ready',
    profile_version: 1,
    app_active: true,
    seniority_hint: null,
    // 1 category, 2 keywords — the API derives these from scoring_profile.
    keyword_count: 2,
    category_count: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-04-30T00:00:00Z',
    ...overrides,
  };
}

const noop = () => undefined;

describe('TargetCard', () => {
  beforeEach(() => {
    mockPush.mockClear();
  });

  it('renders the target label', () => {
    render(
      <TargetCard
        target={makeTarget()}
        fitScore={null}
        fitScoreReasoning={null}
        isActive
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    expect(screen.getByText('Senior Frontend Engineer')).toBeInTheDocument();
  });

  it('renders category and keyword counts from the summary', () => {
    render(
      <TargetCard
        target={makeTarget({ category_count: 3, keyword_count: 17 })}
        fitScore={null}
        fitScoreReasoning={null}
        isActive
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    expect(screen.getByText('3')).toBeInTheDocument();
    expect(screen.getByText('17')).toBeInTheDocument();
  });

  it('shows a fit-score badge when fitScore is provided', () => {
    render(
      <TargetCard
        target={makeTarget()}
        fitScore={92}
        fitScoreReasoning='Great match'
        isActive
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    expect(screen.getByText('92')).toBeInTheDocument();
  });

  it('omits the fit-score badge when fitScore is null', () => {
    render(
      <TargetCard
        target={makeTarget()}
        fitScore={null}
        fitScoreReasoning={null}
        isActive
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    // With fitScore=92 the badge text would be "92"; assert that's absent.
    expect(screen.queryByText('92')).toBeNull();
  });

  it('navigates to /targets/<id> when the card is activated by click', async () => {
    const user = userEvent.setup();
    render(
      <TargetCard
        target={makeTarget()}
        fitScore={null}
        fitScoreReasoning={null}
        isActive
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    const card = screen.getByRole('button', {
      name: /open target senior frontend engineer/i,
    });
    await user.click(card);
    expect(mockPush).toHaveBeenCalledWith('/targets/t-1');
  });

  it('shows an "Active" status when isActive is true', () => {
    render(
      <TargetCard
        target={makeTarget()}
        fitScore={null}
        fitScoreReasoning={null}
        isActive
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    expect(screen.getByText('Active')).toBeInTheDocument();
  });

  it('shows an "Inactive" status when isActive is false', () => {
    render(
      <TargetCard
        target={makeTarget()}
        fitScore={null}
        fitScoreReasoning={null}
        isActive={false}
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    expect(screen.getByText('Inactive')).toBeInTheDocument();
  });

  it('shows a building indicator and dashes counts while deriving', () => {
    render(
      <TargetCard
        target={makeTarget({ activation_status: 'deriving' })}
        fitScore={null}
        fitScoreReasoning={null}
        isActive={false}
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    expect(screen.getByText(/building/i)).toBeInTheDocument();
    // Category/keyword counts are placeholders until derivation completes.
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText('Inactive')).toBeNull();
  });

  it('disables Activate in the dropdown while deriving', async () => {
    const user = userEvent.setup();
    const onActivate = jest.fn();
    const { container } = render(
      <TargetCard
        target={makeTarget({ activation_status: 'deriving' })}
        fitScore={null}
        fitScoreReasoning={null}
        isActive={false}
        onActivate={onActivate}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    const trigger = container.querySelector(
      '[aria-haspopup="menu"]'
    ) as HTMLElement;
    await user.click(trigger);
    const activate = screen.getByRole('menuitem', { name: /activate/i });
    expect(activate).toHaveAttribute('aria-disabled', 'true');
    await user.click(activate);
    expect(onActivate).not.toHaveBeenCalled();
  });

  it('exposes an accessible name on every action-menu item (#837)', async () => {
    // #837 observed three UNNAMED menuitems on prod (2026-08-17) — Delete
    // sat one unlabelled row below Activate for screen-reader users. The
    // defect lived in the shared-ui Dropdown and was fixed by a later bump;
    // re-probed live 2026-09-04 and every item names itself from its label
    // text. This pins the contract so a Dropdown regression (e.g. the label
    // span going aria-hidden, or a content/label refactor) fails HERE
    // instead of resurfacing as an unlabelled destructive action.
    const user = userEvent.setup();
    const { container } = render(
      <TargetCard
        target={makeTarget()}
        fitScore={null}
        fitScoreReasoning={null}
        isActive={false}
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    await user.click(
      container.querySelector('[aria-haspopup="menu"]') as HTMLElement
    );
    const items = screen.getAllByRole('menuitem');
    expect(items).toHaveLength(3);
    // Exact, position-independent names — the destructive one included.
    expect(
      screen.getByRole('menuitem', { name: 'View jobs' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('menuitem', { name: 'Activate' })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('menuitem', { name: 'Delete' })
    ).toBeInTheDocument();
  });

  it('keeps "View jobs" enabled on a deactivated target (saved jobs stay viewable)', async () => {
    const user = userEvent.setup();
    const onViewJobs = jest.fn();
    const { container } = render(
      <TargetCard
        target={makeTarget()}
        fitScore={null}
        fitScoreReasoning={null}
        isActive={false}
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={onViewJobs}
      />
    );
    const trigger = container.querySelector(
      '[aria-haspopup="menu"]'
    ) as HTMLElement;
    await user.click(trigger);
    const viewJobs = screen.getByRole('menuitem', { name: /view jobs/i });
    expect(viewJobs).not.toHaveAttribute('aria-disabled', 'true');
    await user.click(viewJobs);
    expect(onViewJobs).toHaveBeenCalledWith('t-1');
  });

  it('surfaces a failure state when derivation errored', () => {
    render(
      <TargetCard
        target={makeTarget({ activation_status: 'error' })}
        fitScore={null}
        fitScoreReasoning={null}
        isActive={false}
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    expect(screen.getByText(/activation failed/i)).toBeInTheDocument();
  });

  it('names WHY it failed instead of a bare error, per reason code', async () => {
    // #649: the card used to say only "Derivation failed" whether the user
    // had to act or a backend call blipped. Assert the reason reaches the
    // user — that is the whole point of persisting a code.
    const { unmount } = render(
      <TargetCard
        target={makeTarget({
          activation_status: 'error',
          activation_error: 'no_experience_profile',
        })}
        fitScore={null}
        fitScoreReasoning={null}
        isActive={false}
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    expect(screen.getByText(/experience profile/i)).toBeInTheDocument();
    unmount();

    render(
      <TargetCard
        target={makeTarget({
          activation_status: 'error',
          activation_error: 'derive_timeout',
        })}
        fitScore={null}
        fitScoreReasoning={null}
        isActive={false}
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    // A DIFFERENT reason must render DIFFERENT copy, or the mapping is
    // decorative and the code could be ignored entirely.
    expect(screen.getByText(/took too long/i)).toBeInTheDocument();
    expect(screen.queryByText(/experience profile/i)).not.toBeInTheDocument();
  });

  it('falls back to generic copy for an unknown or absent reason code', () => {
    // Older rows predate the column, and the server can add a code before
    // this map catches up — neither may render an empty card.
    render(
      <TargetCard
        target={makeTarget({
          activation_status: 'error',
          activation_error: 'something_invented_later',
        })}
        fitScore={null}
        fitScoreReasoning={null}
        isActive={false}
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    expect(screen.getByText(/something went wrong/i)).toBeInTheDocument();
  });

  it('offers Retry on a failed card and does not navigate when clicked', async () => {
    const user = userEvent.setup();
    const onRetry = jest.fn();
    render(
      <TargetCard
        target={makeTarget({
          activation_status: 'error',
          activation_error: 'pipeline_failed',
        })}
        fitScore={null}
        fitScoreReasoning={null}
        isActive={false}
        onActivate={noop}
        onRetry={onRetry}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    await user.click(screen.getByRole('button', { name: /retry/i }));
    expect(onRetry).toHaveBeenCalledWith('t-1');
    // The whole card is a navigation target; the retry must not also route.
    expect(mockPush).not.toHaveBeenCalled();
  });

  it('shows no Retry affordance unless the target actually failed', () => {
    // Guard against the button leaking onto healthy cards — `ready` is the
    // overwhelmingly common state.
    render(
      <TargetCard
        target={makeTarget({ activation_status: 'ready' })}
        fitScore={null}
        fitScoreReasoning={null}
        isActive
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull();
  });

  /**
   * The card navigated via `router.push` on a `role="button"` div, so there
   * was no cmd/middle-click to open a target in a new tab and no link to copy
   * — everything an anchor gives for free (first sweep C4).
   */
  function renderPlainCard() {
    render(
      <TargetCard
        target={makeTarget()}
        fitScore={null}
        fitScoreReasoning={null}
        isActive
        onActivate={noop}
        onRetry={noop}
        retrying={false}
        onDeactivate={noop}
        onDelete={noop}
        onViewJobs={noop}
      />
    );
  }

  it('exposes the target as a real link, so it can be opened in a new tab', () => {
    renderPlainCard();
    const link = screen.getByRole('link', {
      name: /senior frontend engineer/i,
    });
    expect(link).toHaveAttribute('href', '/targets/t-1');
  });

  it('does not also fire the card push when the link itself is clicked', async () => {
    const user = userEvent.setup();
    renderPlainCard();
    await user.click(
      screen.getByRole('link', { name: /senior frontend engineer/i })
    );
    // The anchor handles navigation; the card's router.push must not also fire.
    expect(mockPush).not.toHaveBeenCalled();
  });
});
