import type { Metadata } from 'next';
import Link from 'next/link';
import LegalDocument, { type LegalSection } from '../_components/LegalDocument';

const LEGAL_EMAIL = 'legal@wyrdfold.com';

export const metadata: Metadata = {
  title: 'Terms of Service — WyrdFold',
  description:
    'The terms that govern your use of WyrdFold, including AI-generated content, subscriptions, and acceptable use.',
  robots: { index: true, follow: true },
};

const SECTIONS: LegalSection[] = [
  {
    heading: 'Agreement to these terms',
    body: (
      <p>
        These Terms of Service (“Terms”) govern your use of WyrdFold, operated
        by [Legal Entity Name] (“we”, “us”). Please read these Terms together
        with our{' '}
        <Link className='underline underline-offset-2' href='/privacy'>
          Privacy Policy
        </Link>
        , which forms part of your agreement with WyrdFold. By creating an
        account or using the service, you agree to these Terms. If you do not
        agree, do not use WyrdFold.
      </p>
    ),
  },
  {
    heading: 'Eligibility',
    body: (
      <p>
        You must be at least 18 years old and able to form a binding contract to
        use WyrdFold. By using the service you confirm that you meet these
        requirements.
      </p>
    ),
  },
  {
    heading: 'What WyrdFold does',
    body: (
      <>
        <p>
          WyrdFold is software designed to assist your job search: it surfaces
          job postings relevant to your profile, scores how well they fit, and
          uses AI to draft tailored resumes and cover letters from the
          experience you provide. It is <b>not</b> a recruiter, employment
          agency, staffing service, career advisor, or legal or professional
          advisor, and no employment relationship is created through your use of
          the service.
        </p>
        <p>
          Job listings originate from third-party sources and may change or be
          removed at any time without notice.
        </p>
      </>
    ),
  },
  {
    heading: 'Your account',
    body: (
      <p>
        You sign in with a one-time magic link sent to your email, so keep
        access to that inbox secure. You are responsible for activity under your
        account. Accounts are for a single individual and may not be shared.
        Tell us promptly if you suspect unauthorized use.
      </p>
    ),
  },
  {
    heading: 'Acceptable use',
    body: (
      <>
        <p>You agree not to:</p>
        <ul className='ml-5 list-disc space-y-1'>
          <li>
            use WyrdFold for any unlawful, misleading, or fraudulent purpose;
          </li>
          <li>
            use the service in violation of applicable export-control or
            sanctions laws;
          </li>
          <li>
            upload data about other people that you do not have the right to
            provide;
          </li>
          <li>
            scrape, resell, or redistribute the service or its data, or exceed
            documented rate limits;
          </li>
          <li>
            reverse-engineer, disrupt, or attempt to gain unauthorized access to
            the service or other users’ data;
          </li>
          <li>
            misrepresent AI-generated content as independently verified fact.
          </li>
        </ul>
      </>
    ),
  },
  {
    heading: 'Your content',
    body: (
      <p>
        You keep ownership of the experience, resume text, and other content you
        provide (“Your Content”). You grant us a limited license to store and
        process Your Content solely to provide the service to you — including
        sending relevant parts to AI providers as described in the{' '}
        <Link className='underline underline-offset-2' href='/privacy'>
          Privacy Policy
        </Link>
        . You are responsible for the accuracy and lawfulness of Your Content.
      </p>
    ),
  },
  {
    heading: 'Feedback',
    body: (
      <p>
        If you send us suggestions, ideas, or feedback about WyrdFold, you grant
        us the right to use that feedback without compensation or restriction.
      </p>
    ),
  },
  {
    heading: 'AI-generated content — no guarantees',
    body: (
      <>
        <p>
          Match scores, role suggestions, and generated documents are produced
          by automated AI systems. They may be{' '}
          <b>inaccurate, incomplete, or unsuitable</b>, and they are provided to
          assist — not replace — your own judgment. AI-generated content may
          resemble content generated for other users and is not guaranteed to be
          unique.
        </p>
        <p>
          <b>
            You are responsible for reviewing and editing every document before
            you use or submit it.
          </b>{' '}
          WyrdFold does <b>not</b> guarantee employment, interviews, responses,
          or the accuracy, quality, or suitability of any match or generated
          content.
        </p>
      </>
    ),
  },
  {
    heading: 'Subscriptions and billing',
    body: (
      <p>
        WyrdFold offers a free tier and paid subscription plans. Paid plans are
        billed in advance on a recurring basis until cancelled. Billing is
        handled by Stripe under its own terms and privacy practices. You can
        cancel at any time from your account; cancellation takes effect at the
        end of the current billing period. Fees are non-refundable except where
        required by law. Prices and taxes are as shown at purchase and may
        change on notice.
      </p>
    ),
  },
  {
    heading: 'Bring-your-own-key (BYOK)',
    body: (
      <p>
        If you supply your own AI provider API key, you authorize us to send
        requests to that provider using it on your behalf. You are responsible
        for all usage and costs incurred on that key and for complying with that
        provider’s terms. If the key is removed, revoked, or becomes invalid, AI
        features may stop working. We store the key only to make requests on
        your behalf and use it for no other purpose.
      </p>
    ),
  },
  {
    heading: 'Intellectual property',
    body: (
      <p>
        The WyrdFold service, software, and brand are owned by us and protected
        by law. These Terms grant you a personal, non-transferable right to use
        the service; they do not transfer any of our intellectual property to
        you.
      </p>
    ),
  },
  {
    heading: 'Copyright complaints',
    body: (
      <p>
        If you believe content on WyrdFold infringes your copyright, contact us
        at{' '}
        <a
          className='underline underline-offset-2'
          href={`mailto:${LEGAL_EMAIL}`}
        >
          {LEGAL_EMAIL}
        </a>{' '}
        with the details, and we will review it.
      </p>
    ),
  },
  {
    heading: 'Third-party services',
    body: (
      <p>
        WyrdFold links to and relies on third-party services (job boards, AI
        providers, and payment processors). We are not responsible for their
        content or availability, and your use of them is governed by their own
        terms.
      </p>
    ),
  },
  {
    heading: 'Beta features',
    body: (
      <p>
        We may offer experimental or beta features. These may be changed or
        discontinued at any time and are provided without warranty.
      </p>
    ),
  },
  {
    heading: 'Disclaimers',
    body: (
      <p>
        The service is provided “as is” and “as available,” without warranties
        of any kind, whether express or implied, including fitness for a
        particular purpose and non-infringement, to the fullest extent permitted
        by law. We do not guarantee that the service will be uninterrupted,
        timely, secure, or error-free — third-party AI providers and
        infrastructure may be unavailable at times.
      </p>
    ),
  },
  {
    heading: 'Limitation of liability',
    body: (
      <p>
        To the fullest extent permitted by law, we will not be liable for any
        indirect, incidental, or consequential damages, or for lost
        opportunities or employment outcomes. Our total liability for any claim
        relating to the service is limited to the greater of the amount you paid
        us in the prior 12 months or USD&nbsp;$100.
      </p>
    ),
  },
  {
    heading: 'Suspension and termination',
    body: (
      <p>
        You may stop using WyrdFold and delete your account at any time. We may
        suspend or terminate access if you breach these Terms or to protect the
        service or other users. On termination, your right to use the service
        ends; sections that by their nature should survive will remain in
        effect. Following account deletion, we may retain certain information
        where required by law, for fraud prevention, dispute resolution, or to
        enforce these Terms — as described in our{' '}
        <Link className='underline underline-offset-2' href='/privacy'>
          Privacy Policy
        </Link>
        .
      </p>
    ),
  },
  {
    heading: 'General',
    body: (
      <ul className='ml-5 list-disc space-y-1'>
        <li>
          <b>Entire agreement.</b> These Terms and the Privacy Policy constitute
          the entire agreement between you and WyrdFold regarding the service.
        </li>
        <li>
          <b>Severability.</b> If any provision is found unenforceable, the
          remainder of these Terms continues in effect.
        </li>
        <li>
          <b>Waiver.</b> Our failure to enforce any provision does not waive our
          right to enforce it later.
        </li>
        <li>
          <b>Assignment.</b> We may assign these Terms in connection with a
          merger, acquisition, or sale of assets.
        </li>
        <li>
          <b>Force majeure.</b> We are not liable for any failure or delay
          caused by events beyond our reasonable control.
        </li>
      </ul>
    ),
  },
  {
    heading: 'Changes to these terms',
    body: (
      <p>
        We may update these Terms as the service evolves. We will post the
        updated version here and, where the change is material, notify you.
        Continuing to use WyrdFold after an update means you accept the revised
        Terms.
      </p>
    ),
  },
  {
    heading: 'Governing law',
    body: (
      <p>
        These Terms are governed by the laws of [Jurisdiction], without regard
        to its conflict-of-laws rules, and any disputes will be resolved in the
        courts of [Jurisdiction].
      </p>
    ),
  },
  {
    heading: 'Contact',
    body: (
      <p>
        Questions about these Terms? Email{' '}
        <a
          className='underline underline-offset-2'
          href={`mailto:${LEGAL_EMAIL}`}
        >
          {LEGAL_EMAIL}
        </a>
        .
      </p>
    ),
  },
];

export default function TermsPage() {
  return (
    <LegalDocument
      title='Terms of Service'
      effective='July 21, 2026'
      intro={
        <p>
          These terms govern your use of WyrdFold. Please read them together
          with our{' '}
          <Link className='underline underline-offset-2' href='/privacy'>
            Privacy Policy
          </Link>
          .
        </p>
      }
      sections={SECTIONS}
    />
  );
}
