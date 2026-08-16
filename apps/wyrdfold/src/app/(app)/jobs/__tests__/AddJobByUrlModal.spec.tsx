import React from 'react';
import '@testing-library/jest-dom';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import AddJobByUrlModal from '../AddJobByUrlModal';

function renderModal(
  overrides: Partial<React.ComponentProps<typeof AddJobByUrlModal>> = {}
) {
  const props: React.ComponentProps<typeof AddJobByUrlModal> = {
    isOpen: true,
    onClose: jest.fn(),
    onSubmit: jest.fn().mockResolvedValue(true),
    submitting: false,
    error: null,
    needsManualFields: false,
    extracted: null,
    ...overrides,
  };
  render(<AddJobByUrlModal {...props} />);
  return props;
}

describe('AddJobByUrlModal', () => {
  it('submits the typed URL', async () => {
    const user = userEvent.setup();
    const props = renderModal();

    await user.type(
      screen.getByLabelText(/job posting url/i),
      'https://x.com/job'
    );
    await user.click(screen.getByRole('button', { name: /add job/i }));

    expect(props.onSubmit).toHaveBeenCalledWith({ url: 'https://x.com/job' });
  });

  // The reason this component replaced window.prompt: a native prompt has
  // nowhere to render this, so the reason for the failure was lost.
  it('renders the failure reason against the URL field', () => {
    renderModal({ error: 'This site blocks automated readers, so we...' });
    expect(screen.getByText(/blocks automated readers/i)).toBeInTheDocument();
  });

  it('keeps the submit disabled until a URL is entered', async () => {
    const user = userEvent.setup();
    renderModal();
    const submit = screen.getByRole('button', { name: /add job/i });

    expect(submit).toBeDisabled();
    await user.type(screen.getByLabelText(/job posting url/i), 'https://x/y');
    expect(submit).toBeEnabled();
  });

  describe('manual fallback', () => {
    it('pre-fills the fields the API did manage to extract', () => {
      renderModal({
        needsManualFields: true,
        extracted: {
          title: null,
          company_name: 'Acme',
          location: 'Remote (US)',
        },
      });

      expect(screen.getByLabelText(/company/i)).toHaveValue('Acme');
      expect(screen.getByLabelText(/location/i)).toHaveValue('Remote (US)');
      expect(screen.getByLabelText(/job title/i)).toHaveValue('');
    });

    it('requires a title before it will re-submit', async () => {
      const user = userEvent.setup();
      renderModal({
        needsManualFields: true,
        extracted: { title: null, company_name: 'Acme', location: null },
      });

      await user.type(
        screen.getByLabelText(/job posting url/i),
        'https://x.com/job'
      );
      const submit = screen.getByRole('button', { name: /add job/i });
      expect(submit).toBeDisabled();

      await user.type(screen.getByLabelText(/job title/i), 'Staff Engineer');
      expect(submit).toBeEnabled();
    });

    it('sends the manual overrides alongside the URL', async () => {
      const user = userEvent.setup();
      const props = renderModal({
        needsManualFields: true,
        extracted: { title: null, company_name: 'Acme', location: null },
      });

      await user.type(
        screen.getByLabelText(/job posting url/i),
        'https://x.com/job'
      );
      await user.type(screen.getByLabelText(/job title/i), 'Staff Engineer');
      await user.click(screen.getByRole('button', { name: /add job/i }));

      expect(props.onSubmit).toHaveBeenCalledWith({
        url: 'https://x.com/job',
        title: 'Staff Engineer',
        company_name: 'Acme',
        location: '',
      });
    });
  });
});
