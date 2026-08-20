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
            <b>Authentication data</b> — sign-in tokens and session identifiers
            used to keep you logged in and to secure your account.
          </li>
          <li>
            <b>Uploaded files</b> — the resumes you upload (PDF or DOCX),
            including work history, education, skills, and contact details.
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
            <b>Match analysis</b> — the fit score for a role, the per-axis
            breakdown behind it (skills, seniority, domain, title), and a
            written explanation of the result. This is generated about your
            career history, shown to you in the app, and never shared with
            employers.
          </li>
          <li>
            <b>Job interactions and application history</b> — roles you save,
            hide, or give feedback on; the targets you follow; your pipeline
            stage for a role and when it changed; and a record of which job
            alerts we have already sent, so we do not send them twice.
          </li>
          <li>
            <b>Preferences</b> — your job-alert and notification settings, and
            the filters and score thresholds you set on your lists.
          </li>
          <li>
            <b>Usage &amp; device data</b> — IP address, browser type, pages
            visited, timestamps, and feature interactions, used to operate and
            improve the service.
          </li>
          <li>
            <b>Billing data</b> — your subscription status and a Stripe customer
            reference. Payment card details go to Stripe rather than being
            stored by WyrdFold.
          </li>
          <li>
            <b>AI provider keys</b> — only if you run a self-hosted WyrdFold and
            supply your own, in which case they are stored encrypted. Not
            applicable to the hosted service, which does not offer this.
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
        own models, and we do not sell it.{' '}
        <b>Improving the service is not the same as training a model on you</b>:
        when we say we improve WyrdFold, we mean noticing that people abandon a
        particular step, or that a page is slow. Your resume and career history
        are not training data.
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
          providers (“<b>AI providers</b>”) through <b>OpenRouter</b>, a service
          that routes requests to them.{' '}
          <b>Only the information necessary to complete your request is sent</b>
          . We have configured our OpenRouter account for{' '}
          <b>Zero-Data-Retention across all model groups</b> and have{' '}
          <b>opted out of provider training</b>. In practice that means requests
          are routed only to providers offering zero-retention processing, and
          not to providers that would train on the content.
        </p>
        <p>
          We should be precise about the limits of that, because it is a
          configuration rather than a promise we can make on another company’s
          behalf. It governs the <i>content</i> of requests. OpenRouter still
          keeps its own record of each request — timing, model, token counts,
          cost — so that we can be billed, and a provider may hold content
          transiently while it is serving the request. We do not control these
          third parties’ policies, and they can change independently of our
          settings. What we control is the configuration above, and we will
          update this page if it changes.
        </p>
        <p>
          <b>
            Bring-your-own-key (BYOK) is not currently offered on the hosted
            service
          </b>
          , so AI features here always run on our provider account. A
          self-hosted WyrdFold can be configured with its own AI provider key,
          in which case data goes directly from that deployment to the provider
          and we neither receive nor process it.
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
            <b>Voyage AI</b> — text embeddings used for matching. Receives job
            postings, role labels, and{' '}
            <b>extracts of your experience profile</b> — job titles, employers,
            dates, skills, and accomplishments — which are turned into numeric
            vectors so we can compare them to roles.{' '}
            <b>
              Your name, email, phone number, address, and profile links are not
              part of those extracts
            </b>
            , and no account identifier is sent with them.
          </li>
          <li>
            <b>Brave Search</b> and <b>Firecrawl</b> — discovering and fetching
            public job postings. These receive job URLs and role-keyword search
            queries (for example a job title plus a careers-site filter). They
            are not sent your resume, your profile, or your identity.
          </li>
          <li>
            <b>Sentry</b> — error monitoring.
          </li>
        </ul>
        <p>
          WyrdFold does not make automated decisions that have legal or
          similarly significant effects on you. All AI-generated content —
          including match scores, resumes, and cover letters — is provided as a
          draft for your review and editing before any submission or use. You
          retain full control over the final content you choose to share with
          employers.
        </p>
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
            <b>Security, fraud prevention, and service reliability</b> — our
            legitimate interests. Those interests are: keeping the service
            secure, preventing abuse and fraud, keeping it reliable and
            improving it, and protecting our legal rights. We balance them
            against your rights, and you can object (see below).
          </li>
          <li>
            <b>Tax, accounting, and responding to lawful requests</b> —
            compliance with a legal obligation.
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
          We share your data with the categories of recipient below, and we do
          not sell it. Those categories are: the sub-processors listed above;
          recipients you direct us to (for example, exporting a document), and
          where required by law — such as in response to a court order,
          subpoena, or a fraud investigation.
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
          delete your account and the data associated with it at any time from{' '}
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
        <p>
          Those periods describe <b>our</b> database and backups. Deletion does
          not travel instantly through every third party: Stripe keeps payment
          records for its own legal and accounting obligations, hosting and
          error-monitoring providers cycle their logs on their own schedules,
          and AI providers process under the arrangement described above. We
          delete what is ours and instruct our processors accordingly; we cannot
          reach into another company’s systems and erase a log by hand.
        </p>
        <p>
          Deletion covers <b>Your Content</b> (your profile and experience data
          and any resumes you upload), <b>Generated Content</b>, saved jobs and
          pipeline history, notification settings, and your account itself.
          Before deleting, you can download all of it from{' '}
          <span className='font-mono text-sm'>Settings → Export my data</span> —
          the export covers the same records the deletion removes.
        </p>
        <p>
          <b>Generated Content</b> — the resumes and cover letters WyrdFold
          produces for you — is yours, as set out in our{' '}
          <Link className='underline underline-offset-2' href='/terms'>
            Terms of Service
          </Link>
          . Deleting your account removes our copies, not any copy you have
          downloaded or already sent.
        </p>
        <p>
          Deleting your account also cancels any active subscription, so billing
          stops — see our{' '}
          <Link className='underline underline-offset-2' href='/terms'>
            Terms of Service
          </Link>{' '}
          for what that means for the period you have already paid for.
        </p>
      </>
    ),
  },
  {
    heading: 'Your rights',
    body: (
      <>
        <p>
          Depending on where you live — for example under the GDPR (EEA/UK) or
          the CCPA/CPRA (California) — you may have the right to:
        </p>
        <ul className='ml-5 list-disc space-y-1'>
          <li>
            <b>Access</b> a copy of the personal data we hold about you.
          </li>
          <li>
            <b>Correct</b> data that is inaccurate or incomplete.
          </li>
          <li>
            <b>Delete</b> your data (“erasure”).
          </li>
          <li>
            <b>Port</b> your data — receive it in a structured, machine-readable
            format. The in-app export produces exactly this.
          </li>
          <li>
            <b>Restrict</b> processing in certain circumstances, for example
            while a correction request is being resolved.
          </li>
          <li>
            <b>Object</b> to processing based on our legitimate interests.
          </li>
          <li>
            <b>Withdraw consent</b> at any time where we rely on it, such as job
            alerts. Withdrawing consent does not affect processing that already
            happened while consent was in place.
          </li>
          <li>
            <b>Complain</b> to your local data-protection authority.
          </li>
        </ul>
        <p>
          You can do most of this yourself in Settings — export, correction, and
          deletion are all self-service — or contact us at{' '}
          <MailLink address={PRIVACY_EMAIL} />. We do not charge for these
          requests or treat you differently for making one.
        </p>
      </>
    ),
  },
  {
    heading: 'California privacy rights',
    body: (
      <>
        <p>
          This section is our notice to California residents under the
          CCPA/CPRA. It restates, in the form that law expects, what the rest of
          this policy already describes.
        </p>
        <p>
          <b>What we collect and where it comes from.</b> The categories are
          listed under <i>Information we collect</i> above: identifiers (email,
          account and session identifiers, IP address), professional and
          employment information (your resumes, derived profile, and match
          analysis), internet activity (pages visited, feature interactions),
          commercial information (subscription status), and the content you
          create in the product. Most of it comes directly from you; the rest is
          generated by the service as you use it, or received from Stripe in the
          case of subscription status.
        </p>
        <p>
          <b>Why we collect it.</b> To provide the service you asked for —
          matching, scoring, and document generation — plus billing, security
          and fraud prevention, service reliability, and legal compliance. These
          are the business and commercial purposes for which we collect each
          category.
        </p>
        <p>
          <b>Who receives it.</b> The sub-processors listed above, plus the
          categories of recipient under <i>How we share data</i>.
        </p>
        <p>
          <b>How long we keep it.</b> See <i>Data retention and deletion</i>.
        </p>
        <p>
          <b>
            We do not sell your personal information, and we do not share it for
            cross-context behavioral advertising
          </b>{' '}
          — as those terms are defined by the CCPA/CPRA. We have not done so in
          the preceding 12 months, and we do not knowingly sell or share the
          personal information of anyone under 16.
        </p>
        <p>
          <b>Your rights and how to use them.</b> You have the rights to know,
          access, delete, correct, and to non-discrimination for exercising
          them. Deletion, correction, and export are self-service in Settings;
          otherwise email <MailLink address={PRIVACY_EMAIL} />. Emailing from
          the address on your account is the usual way we verify a request, and
          we may ask for other information reasonably necessary to confirm your
          identity — for example if you no longer have access to that inbox. We
          generally respond within 45 days, or within the period required by
          applicable law, and where permitted we may extend and will tell you if
          we do. An authorized agent may act for you with your written
          permission and verification of your identity.
        </p>
        <p>
          <b>Appeals.</b> If we deny a request you make under California privacy
          law, you may appeal by replying to our decision or emailing{' '}
          <MailLink address={PRIVACY_EMAIL} /> with “appeal” in the subject. We
          will review it and tell you the outcome, and our reasons, within the
          time the law allows.
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
          advertising or cross-site tracking cookies, and we do not run an
          analytics or session-recording script in your browser.
        </p>
        <ul className='ml-5 list-disc space-y-1'>
          <li>
            <b>Essential &amp; authentication</b> — to keep you signed in and to
            secure the service. These are required for WyrdFold to work.
          </li>
        </ul>
        <p>
          Our error monitoring (Sentry) runs on the <i>server</i> only. It
          records errors our API hits so we can fix them; it sets nothing in
          your browser and does not track you between sites.
        </p>
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
        We operate from the United States, and our sub-processors are primarily
        US-based, so if you are outside the US your data is transferred there
        and processed there. Where the law requires a transfer mechanism, we
        rely on appropriate safeguards such as the Standard Contractual Clauses
        or, where a provider is certified, the EU–US Data Privacy Framework.
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
        you. No system is perfectly secure, but we work to keep your data safe.
        If a security incident affects your personal information, we will
        provide notifications where required by applicable law.
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
