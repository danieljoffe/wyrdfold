import type { Metadata } from 'next';
import Link from 'next/link';
import LegalDocument, { type LegalSection } from '../_components/LegalDocument';
import { LEGAL_ENTITY_ADDRESS, LEGAL_ENTITY_NAME } from '@/lib/legalEntity';

// Placeholder — confirm the real legal-contact mailbox before publishing.
const CONTACT_EMAIL = 'privacy@wyrdfold.com';

export const metadata: Metadata = {
  title: 'Privacy Policy — WyrdFold',
  description:
    'How WyrdFold collects, uses, and protects your data — including how your resume and job information are processed by AI.',
  robots: { index: true, follow: true },
};

const SECTIONS: LegalSection[] = [
  {
    heading: 'Who we are',
    body: (
      <p>
        WyrdFold (“we”, “us”) is operated by {LEGAL_ENTITY_NAME},{' '}
        {LEGAL_ENTITY_ADDRESS}. This policy explains what personal data we
        process when you use WyrdFold and the choices you have. For any privacy
        question, contact us at{' '}
        <a
          className='underline underline-offset-2'
          href={`mailto:${CONTACT_EMAIL}`}
        >
          {CONTACT_EMAIL}
        </a>
        .
      </p>
    ),
  },
  {
    heading: 'Information we collect',
    body: (
      <>
        <p>We collect only what the service needs to run your job search:</p>
        <ul className='ml-5 list-disc space-y-1'>
          <li>
            <b>Account data</b> — your email address (sign-in is a magic link,
            so we do not store a password).
          </li>
          <li>
            <b>Career profile</b> — the experience, resume text, skills, and
            preferences you provide or import, and the roles you save, hide, or
            give feedback on.
          </li>
          <li>
            <b>Generated documents</b> — the tailored resumes and cover letters
            WyrdFold drafts for you.
          </li>
          <li>
            <b>Usage &amp; device data</b> — basic analytics and log data (pages
            visited, actions taken, IP address, browser) used to operate and
            improve the service.
          </li>
          <li>
            <b>Billing data</b> — if you subscribe, your payment is handled by
            Stripe; we receive subscription status, not your full card details.
          </li>
        </ul>
      </>
    ),
  },
  {
    heading: 'How we use your data',
    body: (
      <p>
        We use your data to match you to relevant roles, score how well they
        fit, generate tailored applications from your real experience, send you
        the job alerts you ask for, operate and secure the service, and — if you
        subscribe — manage billing. We do not use your career data to train our
        own models, and we do not sell it.
      </p>
    ),
  },
  {
    heading: 'AI processing and sub-processors',
    body: (
      <>
        <p>
          To score matches and draft applications, the relevant parts of your
          profile and the job text are sent to third-party large-language-model
          providers through <b>OpenRouter</b>. <b>Zero-Data-Retention</b> is
          enabled on that account, so those providers do not retain your content
          after processing your request.
        </p>
        <p>We rely on the following sub-processors to run WyrdFold:</p>
        <ul className='ml-5 list-disc space-y-1'>
          <li>
            <b>Supabase</b> — database, authentication, and file storage.
          </li>
          <li>
            <b>Vercel</b> and <b>Railway</b> — application and API hosting.
          </li>
          <li>
            <b>OpenRouter</b> and the AI model providers it routes to — matching
            and document generation.
          </li>
          <li>
            <b>Stripe</b> — subscription payments.
          </li>
          <li>
            <b>Resend</b> — transactional and alert email.
          </li>
          <li>
            <b>Sentry</b> and <b>Google Analytics</b> — error monitoring and
            product analytics.
          </li>
        </ul>
        <p className='text-text-tertiary'>
          [Confirm this list and each provider’s region/DPA with counsel before
          publishing.]
        </p>
      </>
    ),
  },
  {
    heading: 'Legal bases for processing',
    body: (
      <p>
        Where the GDPR or similar laws apply, we process your data to perform
        our contract with you (running the service), on the basis of your
        consent (e.g. optional alerts and analytics), and for our legitimate
        interests in operating and securing WyrdFold. [Confirm the applicable
        bases for your jurisdiction.]
      </p>
    ),
  },
  {
    heading: 'How we share data',
    body: (
      <p>
        We share your data only with the sub-processors listed above, where you
        direct us to (for example, exporting a document), and where required by
        law. We do not sell your personal data.
      </p>
    ),
  },
  {
    heading: 'Data retention and deletion',
    body: (
      <p>
        We keep your data for as long as your account is active. You can
        permanently delete your account and its data at any time from{' '}
        <span className='font-mono text-sm'>Settings → Delete account</span>,
        which erases your profile, generated documents, and associated records.
        Some records may be retained where the law requires (for example,
        billing records).
      </p>
    ),
  },
  {
    heading: 'Your rights',
    body: (
      <p>
        Depending on where you live, you may have the right to access, correct,
        delete, export, or object to the processing of your personal data, and
        to withdraw consent. You can exercise most of these in-app, or contact
        us at{' '}
        <a
          className='underline underline-offset-2'
          href={`mailto:${CONTACT_EMAIL}`}
        >
          {CONTACT_EMAIL}
        </a>
        . You also have the right to complain to your local data-protection
        authority.
      </p>
    ),
  },
  {
    heading: 'Cookies',
    body: (
      <p>
        We use essential cookies to keep you signed in and to secure the
        service, and analytics cookies to understand usage. You can control
        non-essential cookies through your browser or any consent controls we
        provide.
      </p>
    ),
  },
  {
    heading: 'International transfers',
    body: (
      <p>
        Your data may be processed in countries other than your own, including
        the United States. Where required, we rely on appropriate safeguards
        (such as Standard Contractual Clauses) for those transfers. [Confirm
        with counsel.]
      </p>
    ),
  },
  {
    heading: 'Security',
    body: (
      <p>
        We protect your data with per-user access controls (row-level security),
        encryption in transit, scoped credentials, and regular security review.
        No system is perfectly secure, but we work to keep your data safe.
      </p>
    ),
  },
  {
    heading: 'Children',
    body: (
      <p>
        WyrdFold is not intended for anyone under 18, and we do not knowingly
        collect data from children.
      </p>
    ),
  },
  {
    heading: 'Changes to this policy',
    body: (
      <p>
        We may update this policy as the service evolves. We will post the new
        version here and update the effective date, and will notify you of
        material changes where required.
      </p>
    ),
  },
  {
    heading: 'Contact',
    body: (
      <p>
        Questions about your privacy? Email{' '}
        <a
          className='underline underline-offset-2'
          href={`mailto:${CONTACT_EMAIL}`}
        >
          {CONTACT_EMAIL}
        </a>{' '}
        or write to {LEGAL_ENTITY_NAME}, {LEGAL_ENTITY_ADDRESS}.
      </p>
    ),
  },
];

export default function PrivacyPage() {
  return (
    <LegalDocument
      title='Privacy Policy'
      effective='[Effective date — set on legal sign-off]'
      intro={
        <p>
          This policy explains how WyrdFold handles your personal data. It sits
          alongside our{' '}
          <Link className='underline underline-offset-2' href='/terms'>
            Terms of Service
          </Link>
          .
        </p>
      }
      sections={SECTIONS}
    />
  );
}
