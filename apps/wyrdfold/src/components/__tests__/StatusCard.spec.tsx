import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import { expectNoA11yViolations } from '@/test-utils/axe';
import StatusCard from '../StatusCard';
import LinkButton from '@/components/kit/LinkButton';

// The one shared status-card composition (#971) — four surfaces render it
// (root/app/search not-found + the app error boundary), so a change here is
// a change to all of them. Pin the parts every surface relies on.
describe('StatusCard', () => {
  function renderCard() {
    return render(
      <StatusCard
        title='Nothing here'
        body='The thing you wanted is gone.'
        actions={
          <LinkButton name='t-home' variant='primary' size='sm' href='/x'>
            Go somewhere
          </LinkButton>
        }
      />
    );
  }

  it('renders the title as the page h1, the body, and the actions', () => {
    renderCard();
    expect(
      screen.getByRole('heading', { level: 1, name: 'Nothing here' })
    ).toBeInTheDocument();
    expect(
      screen.getByText('The thing you wanted is gone.')
    ).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Go somewhere' })).toHaveAttribute(
      'href',
      '/x'
    );
  });

  it('has no axe violations', async () => {
    const { container } = renderCard();
    await expectNoA11yViolations(container);
  });
});
