import '@testing-library/jest-dom';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { expectNoA11yViolations } from '@/test-utils/axe';
import WaitlistForm from '../WaitlistForm';

const ORIGINAL_FETCH = global.fetch;

function mockFetch(impl: () => Promise<Partial<Response>>): jest.Mock {
  const fn = jest.fn().mockImplementation(impl);
  global.fetch = fn as unknown as typeof fetch;
  return fn;
}

afterAll(() => {
  global.fetch = ORIGINAL_FETCH;
});

beforeEach(() => {
  jest.clearAllMocks();
});

describe('WaitlistForm', () => {
  it('renders the email field and join button', () => {
    render(<WaitlistForm />);
    expect(
      screen.getByRole('textbox', { name: /email address/i })
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /join the waitlist/i })
    ).toBeInTheDocument();
  });

  it('has no axe violations in the idle state', async () => {
    const { container } = render(<WaitlistForm />);
    await expectNoA11yViolations(container);
  });

  it('masks the email input for Sentry PII redaction', () => {
    render(<WaitlistForm />);
    expect(
      screen.getByRole('textbox', { name: /email address/i })
    ).toHaveAttribute('data-sentry-mask');
  });

  it('shows a success state after a successful submit', async () => {
    const fetchMock = mockFetch(async () => ({ ok: true }));
    const user = userEvent.setup();
    render(<WaitlistForm />);

    await user.type(
      screen.getByRole('textbox', { name: /email address/i }),
      'jane@example.com'
    );
    await user.click(
      screen.getByRole('button', { name: /join the waitlist/i })
    );

    await waitFor(() =>
      expect(screen.getByRole('status')).toHaveTextContent(/on the list/i)
    );
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/waitlist',
      expect.objectContaining({ method: 'POST' })
    );
    // Form (and its email field) is gone in the success state.
    expect(
      screen.queryByRole('textbox', { name: /email address/i })
    ).not.toBeInTheDocument();
  });

  it('rejects an invalid email client-side without calling the API', async () => {
    const fetchMock = mockFetch(async () => ({ ok: true }));
    const user = userEvent.setup();
    render(<WaitlistForm />);

    await user.type(
      screen.getByRole('textbox', { name: /email address/i }),
      'not-an-email'
    );
    await user.click(
      screen.getByRole('button', { name: /join the waitlist/i })
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(/valid email/i);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('surfaces a server error message and stays on the form', async () => {
    mockFetch(async () => ({
      ok: false,
      json: async () => ({
        error: 'Too many requests. Please try again later.',
      }),
    }));
    const user = userEvent.setup();
    render(<WaitlistForm />);

    await user.type(
      screen.getByRole('textbox', { name: /email address/i }),
      'jane@example.com'
    );
    await user.click(
      screen.getByRole('button', { name: /join the waitlist/i })
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /too many requests/i
    );
    expect(
      screen.getByRole('textbox', { name: /email address/i })
    ).toBeInTheDocument();
  });

  it('shows a generic error when the network request throws', async () => {
    mockFetch(async () => {
      throw new Error('network down');
    });
    const user = userEvent.setup();
    render(<WaitlistForm />);

    await user.type(
      screen.getByRole('textbox', { name: /email address/i }),
      'jane@example.com'
    );
    await user.click(
      screen.getByRole('button', { name: /join the waitlist/i })
    );

    expect(await screen.findByRole('alert')).toHaveTextContent(
      /something went wrong/i
    );
  });
});
