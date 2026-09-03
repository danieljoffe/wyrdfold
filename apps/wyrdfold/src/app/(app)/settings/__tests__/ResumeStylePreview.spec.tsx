import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { ResumeStylePreview } from '../ResumeStylePreview';

// #844 §4: the preview's whole job is "see how YOUR export will look" —
// it rendered a placeholder person while the real identity sat one route
// away on /profile.
describe('ResumeStylePreview identity', () => {
  it('renders the real identity when provided', () => {
    render(
      <ResumeStylePreview
        preset='classic'
        accent='slate'
        identity={{
          name: 'Daniel Joffe',
          email: 'dan@example.org',
          location: 'Brooklyn, NY',
        }}
      />
    );
    expect(screen.getByText('Daniel Joffe')).toBeInTheDocument();
    expect(
      screen.getByText('Brooklyn, NY · dan@example.org')
    ).toBeInTheDocument();
    expect(screen.queryByText(/Name LastName/)).not.toBeInTheDocument();
  });

  it('falls back per FIELD when identity is partial or absent', () => {
    render(
      <ResumeStylePreview
        preset='classic'
        accent='slate'
        identity={{ name: 'Daniel Joffe', email: '', location: null }}
      />
    );
    expect(screen.getByText('Daniel Joffe')).toBeInTheDocument();
    // Missing fields keep the sample so the layout always previews fully.
    expect(
      screen.getByText('Remote, USA · user@example.com')
    ).toBeInTheDocument();
  });

  it('renders the full sample with no identity at all (fresh account)', () => {
    render(<ResumeStylePreview preset='classic' accent='slate' />);
    expect(screen.getByText('Name LastName')).toBeInTheDocument();
  });
});
