import type { Metadata } from 'next';
import Link from 'next/link';
import LegalDocument, { type LegalSection } from '../_components/LegalDocument';
import { LEGAL_ENTITY_ADDRESS, LEGAL_ENTITY_NAME } from '@/lib/legalEntity';

const PRIVACY_EMAIL = 'privacy@wyrdfold.com';
const LEGAL_EMAIL = 'legal@wyrdfold.com';

export const metadata: Metadata = {
  title: 'Privacy Policy — WyrdFold',
  description:
    'How WyrdFold collects, uses, and protects your data — including how your resume and job information are processed by AI.',
  robots: { index: true, follow: true },
};

function MailLink({ address }: { address: string }) {
  return (
    <a className='underline underline-offset-2' href={`mailto:${address}`}>
      {address}
    </a>
  );
}

const SECTIONS: LegalSection[] = [
  {
    heading: 'Who we are',
    body: (
      <p>
        WyrdFold (“we”, “us”) is operated by {LEGAL_ENTITY_NAME},{' '}
        {LEGAL_ENTITY_ADDRESS}. This policy explains what personal data we
        process when you use WyrdFold and the choices you have. For any privacy
        question, contact us at <MailLink address={PRIVACY_EMAIL} />.
      </p>
    ),
  },
  {
    heading: 'Information we collect',
    body: (
      <>
        <p>
          We collect the following categories of personal data to operate the
          service:
        </p>
        <ul className='ml-5 list-disc space-y-1'>
          <li>
            <b>Account data</b> — your email address. We use magic-link sign-in,
            so we do not store a password.
          </li>
          <li>
            <b>Uploaded resumes</b> — including work history, education, skills,
            and contact details.
          </li>
          <li>
            <b>Derived experience documents</b> — structured profiles extracted
            from your resumes to power our matching engine.
          </li>
          <li>
            <b>Conversation transcripts</b> — your interactions with our AI
            assistant during the job-search process.
          </li>
          <li>
            <b>Generated outputs</b> — tailored resumes, cover letters, and
            match scores produced for you.
          </li>
          <li>
            <b>AI reasoning logs</b> — internal scoring and rationale generated
            about your career history and job fit, used to rank matches and
            never shared with employers.
          </li>
          <li>
            <b>Job interactions</b> — roles you save, hide, or give feedback on.
          </li>
          <li>
            <b>Usage &amp; device data</b> — IP address, browser type, pages
            visited, timestamps, and feature interactions, used to operate and
            improve the service.
          </li>
          <li>
            <b>Billing data</b> — subscription status via Stripe. We do not
            receive or store your full payment card details.
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
          providers through <b>OpenRouter</b> (a service that securely routes
          requests to AI model providers).{' '}
          <b>Only the information necessary to complete your request is sent</b>
          . We have requested <b>Zero-Data-Retention</b> from our providers and
          are in the process of confirming its activation.
        </p>
        <p>
          If you provide your own AI provider API key (BYOK), we use it only to
          make requests on your behalf and for no other purpose.
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
            <b>Voyage AI</b> — text embeddings used for job matching. Receives
            job-posting text and role labels, not your resume.
          </li>
          <li>
            <b>Brave Search</b> and <b>Firecrawl</b> — discovering and fetching
            public job postings. These receive job URLs and search queries, not
            your personal data.
          </li>
          <li>
            <b>Sentry</b> — error monitoring.
          </li>
        </ul>
      </>
    ),
  },
  {
    heading: 'Legal bases for processing',
    body: (
      <>
        <p>
          Where the GDPR or similar laws apply, we rely on the following legal
          bases:
        </p>
        <ul className='ml-5 list-disc space-y-1'>
          <li>
            <b>Running the service</b> (matching, generation, account) —
            performance of our contract with you.
          </li>
          <li>
            <b>Billing and receipts</b> — performance of our contract.
          </li>
          <li>
            <b>Security and fraud prevention</b> — our legitimate interests.
          </li>
          <li>
            <b>Analytics</b> — your consent, where required.
          </li>
          <li>
            <b>Optional job-alert emails</b> — your consent.
          </li>
        </ul>
      </>
    ),
  },
  {
    heading: 'How we share data',
    body: (
      <>
        <p>
          We share your data only with the sub-processors listed above, where
          you direct us to (for example, exporting a document), and where
          required by law — such as in response to a court order, subpoena, or a
          fraud investigation.
        </p>
        <p>
          If WyrdFold is acquired, merged, or sells assets, your data may
          transfer as part of that transaction.{' '}
          <b>We do not sell your personal data</b>, and we do not use your
          personal information for cross-context behavioral advertising.
        </p>
      </>
    ),
  },
  {
    heading: 'Data retention and deletion',
    body: (
      <>
        <p>
          We keep your data for as long as your account is active. You can
          permanently delete your account and its data at any time from{' '}
          <span className='font-mono text-sm'>Settings → Delete account</span>.
          Indicative retention periods:
        </p>
        <ul className='ml-5 list-disc space-y-1'>
          <li>
            when you delete your account via Settings, all data associated with
            it is removed from our database immediately;
          </li>
          <li>
            backups that may still contain the data are cycled out within 30
            days;
          </li>
          <li>
            billing records are retained for up to 7 years where required by
            law.
          </li>
        </ul>
        <p>Generated resumes and cover letters remain your content.</p>
      </>
    ),
  },
  {
    heading: 'Your rights',
    body: (
      <p>
        Depending on where you live, you may have the right to access, correct,
        delete, export, or object to the processing of your personal data, and
        to withdraw consent — for example under the GDPR (EEA/UK) or the
        CCPA/CPRA (California). You can exercise most of these in-app, or
        contact us at <MailLink address={PRIVACY_EMAIL} />. You also have the
        right to complain to your local data-protection authority.
      </p>
    ),
  },
  {
    heading: 'California privacy rights',
    body: (
      <>
        <p>
          If you are a California resident, the CCPA/CPRA gives you the right to
          know what personal information we collect and how we use it, to
          request access to or deletion of it, to correct it, and not to be
          discriminated against for exercising these rights.
        </p>
        <p>
          We do not sell your personal information and do not use it for
          cross-context behavioral advertising. To exercise your rights, contact
          us at <MailLink address={PRIVACY_EMAIL} />.
        </p>
      </>
    ),
  },
  {
    heading: 'Cookies and tracking',
    body: (
      <>
        <p>
          We use only the cookies the service needs to function. We do not use
          advertising or cross-site tracking cookies.
        </p>
        <ul className='ml-5 list-disc space-y-1'>
          <li>
            <b>Essential &amp; authentication</b> — to keep you signed in and to
            secure the service. These are required for WyrdFold to work.
          </li>
        </ul>
        <p>
          You can control cookies through your browser. WyrdFold does not
          currently respond to browser “Do Not Track” signals.
        </p>
      </>
    ),
  },
  {
    heading: 'International transfers',
    body: (
      <p>
        Your data may be processed in countries other than your own, including
        the United States. Where required, we rely on appropriate safeguards
        (such as Standard Contractual Clauses) for those transfers.
      </p>
    ),
  },
  {
    heading: 'Security',
    body: (
      <p>
        We protect your data with technical and organizational safeguards,
        including encryption in transit, access controls, and least-privilege
        permissions. Because sign-in is a magic link, we encourage you to
        protect access to your email account, since it is used to authenticate
        you. No system is perfectly secure, but we work to keep your data safe,
        and if required by law we will notify affected users of a personal data
        breach.
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
        Questions about your privacy? Email <MailLink address={PRIVACY_EMAIL} />{' '}
        (privacy) or <MailLink address={LEGAL_EMAIL} /> (legal), or write to{' '}
        {LEGAL_ENTITY_NAME}, {LEGAL_ENTITY_ADDRESS}.
      </p>
    ),
  },
];

export default function PrivacyPage() {
  return (
    <LegalDocument
      title='Privacy Policy'
      effective='July 21, 2026'
      intro={
        <p>
          This Privacy Policy explains how WyrdFold handles your personal data.
          It should be read together with our{' '}
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
