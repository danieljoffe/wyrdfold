import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import ScoringProfileView from '../ScoringProfileView';
import type { JobTarget, ScoringProfile } from '../../types';

// SEC-2 (#366): the shared scoring model is view-only per user. These pin
// that the view (a) surfaces the full profile and (b) exposes NO editing
// affordance — no inputs, no buttons, no save bar — so a co-searcher can
// read but never rewrite the rubric everyone shares.

const PROFILE: ScoringProfile = {
  categories: {
    frontend: { keywords: { react: 3, typescript: 2 }, weight: 2 },
  },
  seniority: { level: 'senior', signals: ['lead', 'staff'] },
  domain: { signals: ['fintech'], weight: 1.5 },
  negative: { keywords: ['php'], weight: -20 },
};

function makeTarget(scoring_profile: ScoringProfile | null): JobTarget {
  return {
    id: 't-1',
    label: 'Senior Frontend Engineer',
    description: null,
    normalized_label: null,
    scoring_profile: scoring_profile as ScoringProfile,
    search_keywords: [],
    activation_status: 'ready',
    profile_version: 1,
    app_active: true,
    created_at: '2026-01-01',
    updated_at: '2026-01-01',
  };
}

describe('ScoringProfileView', () => {
  it('renders the shared profile: categories, keywords, seniority, domain, penalties', () => {
    render(<ScoringProfileView target={makeTarget(PROFILE)} />);

    expect(screen.getByText('frontend')).toBeInTheDocument();
    expect(screen.getByText('react')).toBeInTheDocument();
    expect(screen.getByText('typescript')).toBeInTheDocument();
    expect(screen.getByText('Level: senior')).toBeInTheDocument();
    expect(screen.getByText('lead')).toBeInTheDocument();
    expect(screen.getByText('fintech')).toBeInTheDocument();
    expect(screen.getByText('php')).toBeInTheDocument();
    // Weights are shown as read-only text (not <input>).
    expect(screen.getByText('Weight 2')).toBeInTheDocument();
    expect(screen.getByText('Weight -20')).toBeInTheDocument();
  });

  it('exposes no editing affordances (no inputs, buttons, or save bar)', () => {
    render(<ScoringProfileView target={makeTarget(PROFILE)} />);

    expect(screen.queryAllByRole('textbox')).toHaveLength(0);
    expect(screen.queryAllByRole('spinbutton')).toHaveLength(0);
    expect(screen.queryAllByRole('button')).toHaveLength(0);
    expect(screen.queryByText(/save/i)).toBeNull();
    expect(screen.queryByText(/unsaved changes/i)).toBeNull();
  });

  it('shows a placeholder when the target has no scoring profile yet', () => {
    render(<ScoringProfileView target={makeTarget(null)} />);
    expect(
      screen.getByText(/doesn.t have a scoring profile yet/i)
    ).toBeInTheDocument();
  });
});
