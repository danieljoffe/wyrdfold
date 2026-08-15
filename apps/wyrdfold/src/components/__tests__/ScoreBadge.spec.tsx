import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import ScoreBadge from '../ScoreBadge';

describe('ScoreBadge', () => {
  it('renders the score as a circular chip (rounded-full, square, no horizontal padding)', () => {
    render(<ScoreBadge score={87} />);
    const badge = screen.getByText('87');
    expect(badge).toHaveClass('rounded-full');
    expect(badge).toHaveClass('aspect-square');
    expect(badge).toHaveClass('p-0');
    // No leftover pill rounding from the base Badge.
    expect(badge).not.toHaveClass('rounded-md');
  });

  it('exposes the score via an accessible name', () => {
    render(<ScoreBadge score={42} />);
    expect(screen.getByLabelText('Match score 42')).toBeInTheDocument();
  });

  // `pending={undefined}` throughout this block is deliberate: it selects the
  // scoring_status fallback (`pending ?? status !== 'complete'`), which is the
  // path these cases exist to pin. The union (#603) requires the flag to be
  // named alongside the status so a call site can't drop it by accident.
  it('renders a scoring spinner only while scoring is in flight', () => {
    const { rerender } = render(
      <ScoreBadge score={50} scoringStatus='scoring' pending={undefined} />
    );
    expect(screen.getByLabelText(/scoring in progress/i)).toBeInTheDocument();

    rerender(
      <ScoreBadge score={50} scoringStatus='complete' pending={undefined} />
    );
    expect(screen.queryByLabelText(/scoring in progress/i)).toBeNull();

    rerender(<ScoreBadge score={50} />);
    expect(screen.queryByLabelText(/scoring in progress/i)).toBeNull();
  });

  it('hides the placeholder number while ungraded, showing a pending chip', () => {
    // stage1/stage2 carry only a keyword placeholder — it must NOT be shown as
    // a graded fit score (#47).
    render(
      <ScoreBadge score={80} scoringStatus='stage2' pending={undefined} />
    );
    expect(screen.queryByText('80')).toBeNull();
    expect(screen.getByLabelText('Match score pending')).toBeInTheDocument();
  });

  it('shows the real number once graded (complete)', () => {
    render(
      <ScoreBadge score={80} scoringStatus='complete' pending={undefined} />
    );
    expect(screen.getByText('80')).toBeInTheDocument();
    expect(screen.getByLabelText('Match score 80')).toBeInTheDocument();
  });

  it('shows a symbol when pending=true even if status is complete (ungraded row)', () => {
    // The 'complete'-but-never-graded case: status says complete but there's no
    // real fit grade. `pending` (fit_reasoning-derived) is authoritative — the
    // keyword placeholder must never surface as a fit score, and no spinner
    // since the row isn't actively scoring.
    render(<ScoreBadge score={100} scoringStatus='complete' pending={true} />);
    expect(screen.queryByText('100')).toBeNull();
    expect(screen.getByLabelText('Match score pending')).toBeInTheDocument();
    expect(screen.queryByLabelText(/scoring in progress/i)).toBeNull();
  });

  it('shows the number when pending=false (a real grade)', () => {
    render(<ScoreBadge score={72} scoringStatus='complete' pending={false} />);
    expect(screen.getByText('72')).toBeInTheDocument();
  });

  /**
   * The app shows TWO scores on different scales, within two clicks of each
   * other: a target's fit against your experience, and a job's match against a
   * target. This chip used to hardcode "Match score" into its accessible name
   * while accepting a caller-supplied tooltip — so on a target card a sighted
   * user read "Fit score 82" and a screen-reader user heard "Match score 82"
   * off the same element.
   */
  describe('score kind', () => {
    it('defaults to match — four of the five call sites are job chips', () => {
      render(<ScoreBadge score={91} />);
      expect(screen.getByLabelText('Match score 91')).toBeInTheDocument();
    });

    it('announces a target chip as a fit score', () => {
      render(<ScoreBadge score={82} kind='fit' />);
      expect(screen.getByLabelText('Fit score 82')).toBeInTheDocument();
      expect(screen.queryByLabelText('Match score 82')).toBeNull();
    });

    it('keeps the pending label in the same vocabulary as the chip', () => {
      const { unmount } = render(
        <ScoreBadge score={80} kind='fit' scoringStatus='stage2' pending />
      );
      expect(screen.getByLabelText('Fit score pending')).toBeInTheDocument();
      unmount();

      render(
        <ScoreBadge score={80} kind='match' scoringStatus='stage2' pending />
      );
      expect(screen.getByLabelText('Match score pending')).toBeInTheDocument();
    });

    it('does not let the tooltip and the accessible name disagree', () => {
      // The exact defect: a "Fit score …" tooltip over a "Match score …" name.
      render(
        <ScoreBadge score={82} kind='fit' title='Fit score 82 — how well…' />
      );
      const chip = screen.getByLabelText('Fit score 82');
      expect(chip).toHaveAttribute('title', 'Fit score 82 — how well…');
    });
  });
});
