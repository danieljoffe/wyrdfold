import type { Metadata } from 'next';
import Link from 'next/link';
import LegalDocument, { type LegalSection } from '../_components/LegalDocument';
import { LEGAL_ENTITY_ADDRESS, LEGAL_ENTITY_NAME } from '@/lib/legalEntity';

const LEGAL_EMAIL = 'legal@wyrdfold.com';

export const metadata: Metadata = {
  title: 'Terms of Service — WyrdFold',
  description:
    'The terms that govern your use of WyrdFold, including AI-generated content, subscriptions, and acceptable use.',
  robots: { index: true, follow: true },
};

function PrivacyLink({ children }: { children?: React.ReactNode }) {
  return (
    <Link className='underline underline-offset-2' href='/privacy'>
      {children ?? 'Privacy Policy'}
    </Link>
  );
}

const SECTIONS: LegalSection[] = [
  {
    heading: 'Agreement to these terms',
    body: (
      <>
        <p>
          These Terms of Service (“Terms”) govern your use of the hosted
          WyrdFold service at wyrdfold.com. The service is operated by{' '}
          <b>{LEGAL_ENTITY_NAME}</b>, a sole proprietor located in California,
          United States, at {LEGAL_ENTITY_ADDRESS}, doing business as WyrdFold
          (“we”, “us”). <b>{LEGAL_ENTITY_NAME} is the contracting party</b>{' '}
          under these Terms; “WyrdFold” is a trading name for that business, not
          a separate company, corporation, or limited-liability entity. You can
          reach us at{' '}
          <a
            className='underline underline-offset-2'
            href={`mailto:${LEGAL_EMAIL}`}
          >
            {LEGAL_EMAIL}
          </a>
          .
        </p>
        <p>
          By creating an account or using the service, you agree to these Terms.
          If you do not agree, do not use WyrdFold. Our <PrivacyLink /> explains
          how we collect, use, disclose, and retain personal information in
          connection with the service.
        </p>
      </>
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
          WyrdFold is software that assists your job search: it surfaces job
          postings relevant to your profile, scores how well they fit, and uses
          AI to draft tailored resumes and cover letters from the experience you
          provide.
        </p>
        <p>
          WyrdFold is <b>not</b> a recruiter, employment agency, staffing
          service, career advisor, or legal or professional advisor. We are not
          your employer, not an employer of anyone, not a representative or
          agent of any employer, and not your agent. We do not act on your
          behalf with employers, and no employment or agency relationship is
          created through your use of the service.
        </p>
      </>
    ),
  },
  {
    heading: 'Job listings and third-party content',
    body: (
      <>
        <p>
          Job listings shown in WyrdFold originate from third-party sources —
          applicant tracking systems, job boards, and public company pages. We
          do not create them, control them, or verify them.
        </p>
        <p>
          <b>
            We do not guarantee that any listing is still open, accurate,
            complete, legitimate, or authorized by the employer it names.
          </b>{' '}
          Listings can be expired, duplicated, mis-described, altered after we
          fetched them, or fraudulent. Compensation and requirements shown may
          differ from the employer’s own posting.
        </p>
        <p>
          <b>
            Verify any role on the employer’s official site before you apply
          </b>
          , and treat anything asking for payment or sensitive personal or
          financial information as suspect.
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
            use the service to fabricate credentials — including employment
            history, education, certifications, licences, or skills you do not
            hold — or to knowingly submit false or misleading information to an
            employer;
          </li>
          <li>impersonate another person or misrepresent your identity;</li>
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
            the service or other users’ data.
          </li>
        </ul>
        <p>
          When you interact with a third-party site through or alongside
          WyrdFold — an employer’s careers page, an applicant tracking system, a
          job board — you must comply with that site’s own terms. Nothing in
          these Terms authorizes you to bypass CAPTCHAs, authentication, access
          controls, anti-bot measures, robots restrictions, or rate limits on
          any third-party service, and we do not authorize you to do so.
        </p>
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
        <PrivacyLink />. You are responsible for the accuracy and lawfulness of
        Your Content.
      </p>
    ),
  },
  {
    heading: 'Generated content',
    body: (
      <>
        <p>
          “Generated Content” means the resumes, cover letters, and similar
          documents WyrdFold produces from Your Content at your request.
        </p>
        <p>
          <b>
            We claim no ownership of Generated Content — as between you and us,
            it is yours.
          </b>{' '}
          To the extent we hold any rights in it, we assign them to you. You may
          use it for any lawful purpose, including submitting it to employers.
          Note that AI-assisted output may resemble output generated for other
          users and is not guaranteed to be unique, and that rights in
          AI-assisted material can be uncertain under copyright law.
        </p>
        <p>
          <b>You are responsible for every document before you use it.</b> That
          means reviewing it, correcting inaccuracies, confirming that every
          claim in it is truthful, confirming that you actually hold the
          qualifications and experience it describes, and deciding whether and
          how to submit it. WyrdFold drafts from what you give it; it does not
          verify any of it.
        </p>
      </>
    ),
  },
  {
    heading: 'AI features and their limitations',
    body: (
      <>
        <p>
          Match scores, role suggestions, and generated documents are produced
          with the assistance of automated AI systems, working from information
          you provide and from third-party job listings. They may be{' '}
          <b>inaccurate, incomplete, or unsuitable</b>, and they are provided to
          assist — not replace — your own judgment.
        </p>
        <p>
          <b>
            Match scores and recommendations are informational tools based on
            the information available to WyrdFold. They are not determinations
            of your qualifications, your employability, or your likelihood of
            being hired
          </b>
          , and they are not an assessment of your professional worth. A low
          score means our software matched a posting poorly against the profile
          you gave it; it means nothing about you.
        </p>
        <p>
          WyrdFold does not make employment decisions, and does not make them on
          behalf of any employer. Hiring decisions are made by employers, using
          their own processes, which we neither see nor influence. We do not
          guarantee employment, interviews, or responses, and we are not
          responsible for any hiring decision made by a third party.
        </p>
      </>
    ),
  },
  {
    heading: 'No professional advice',
    body: (
      <>
        <p>
          WyrdFold provides decision support for your job search. It does not
          provide professional advice of any kind — career, legal, financial,
          immigration, or otherwise.
        </p>
        <p>
          Job matches are not career advice. Information about employers, roles,
          locations, or compensation can be wrong or out of date, because it
          comes from third parties. Resume and cover-letter content is not
          guaranteed accurate. You are responsible for independently verifying
          anything that matters to you, and for the decisions you make based on
          the service.
        </p>
      </>
    ),
  },
  {
    heading: 'Intellectual property',
    body: (
      <p>
        The WyrdFold service, software, and brand are owned by us and protected
        by law. These Terms grant you a personal, non-transferable right to use
        the hosted service; they do not transfer any of our intellectual
        property to you. This section does not affect your rights in Your
        Content or Generated Content, or any rights granted to you by the
        open-source license described below.
      </p>
    ),
  },
  {
    heading: 'Feedback',
    body: (
      <p>
        If you send us suggestions, ideas, or feedback about WyrdFold, we may
        use it to improve, develop, and operate the service without compensation
        or attribution. This does not transfer ownership of your intellectual
        property to us.
      </p>
    ),
  },
  {
    heading: 'Subscriptions, billing, and cancellation',
    body: (
      <>
        <p>
          WyrdFold requires a paid subscription to access its AI-powered
          features. There is no free tier or trial period. Subscriptions are
          billed in advance on a recurring basis until cancelled. Billing is
          handled by Stripe under its own terms and privacy practices — payment
          card information goes to Stripe rather than being stored by WyrdFold.
        </p>
        <p>
          <b>Cancelling.</b> You can cancel at any time from your account
          settings. Cancelling stops future charges and takes effect at the end
          of the billing period you have already paid for — you keep access
          until then, and we do not pro-rate the remainder.
        </p>
        <p>
          <b>Deleting your account cancels your subscription.</b> If you delete
          your account, we cancel any active subscription as part of the same
          action, so you are not billed again. Deletion removes your account and
          its data straight away, so access ends immediately rather than running
          to the end of the period — and we do not refund the unused remainder.{' '}
          <b>
            If you want to keep using WyrdFold until the period you have paid
            for runs out, cancel your subscription rather than deleting your
            account.
          </b>
        </p>
        <p>
          <b>Failed payments.</b> If a payment cannot be collected, Stripe may
          retry it. We may suspend access to paid features while a payment is
          outstanding, and may terminate the subscription if it remains
          uncollected.
        </p>
        <p>
          <b>Price changes.</b> We may change prices on 30 days’ notice. A new
          price applies to the first renewal that falls after the effective
          date, never to a period you have already paid for. If you do not want
          to continue at the new price, cancel before that renewal.
        </p>
        <p>
          <b>Taxes.</b> Prices are shown at purchase. Any sales, use, VAT, GST,
          or similar taxes are determined by your billing location and
          applicable law, and may be added at checkout or on renewal.
        </p>
        <p>
          <b>Refunds.</b> Fees are non-refundable except where required by
          applicable law. We may still issue a refund at our discretion — for a
          duplicate charge, an accidental purchase, or a billing error on our
          side. Ask us at{' '}
          <a
            className='underline underline-offset-2'
            href={`mailto:${LEGAL_EMAIL}`}
          >
            {LEGAL_EMAIL}
          </a>
          .
        </p>
      </>
    ),
  },
  {
    heading: 'Third-party services',
    body: (
      <p>
        WyrdFold links to and relies on third-party services — job boards and
        applicant tracking systems, AI providers, hosting and database
        providers, and a payment processor. We are not responsible for their
        content, availability, or practices, and your use of them is governed by
        their own terms. The <PrivacyLink /> lists the providers that process
        personal information on our behalf.
      </p>
    ),
  },
  {
    heading: 'Self-hosting and open-source software',
    body: (
      <>
        <p>
          WyrdFold’s source code is published under the Functional Source
          License, version 1.1, with an Apache 2.0 future license
          (FSL-1.1-ALv2). Three things are deliberately separate:
        </p>
        <ul className='ml-5 list-disc space-y-1'>
          <li>
            <b>The hosted service at wyrdfold.com</b> is governed by these
            Terms.
          </li>
          <li>
            <b>The source code</b> is governed by the FSL — not by these Terms.
            Your rights to use, modify, and self-host the code come from that
            license, and nothing here grants or removes them.
          </li>
          <li>
            <b>Any self-hosted deployment</b> — yours or a third party’s — is
            not governed by these Terms. We do not operate it, support it,
            approve it, or bear responsibility for it, and no agreement you form
            with its users involves us.
          </li>
        </ul>
        <p>
          The FSL imposes additional restrictions on certain commercial uses,
          including offering a competing hosted service. These Terms do not
          interpret, extend, or limit that license —{' '}
          <b>
            refer to the FSL text itself for the complete terms governing the
            source code
          </b>
          , published in the repository alongside the code it covers.
        </p>
        <p>
          <b>Bring-your-own-key (BYOK).</b> A self-hosted deployment can be
          configured with its own AI provider API key, in which case data goes
          directly from that deployment to the AI provider and we neither
          receive nor process it.{' '}
          <b>
            BYOK is not currently offered on the hosted service at wyrdfold.com
          </b>
          , and nothing in these Terms should be read as a commitment to offer
          it.
        </p>
      </>
    ),
  },
  {
    heading: 'Beta features',
    body: (
      <p>
        We may offer features labelled beta or experimental. These may be
        changed or withdrawn at any time and are provided without warranty.
      </p>
    ),
  },
  {
    heading: 'Changes to the service',
    body: (
      <p>
        WyrdFold is actively developed. We may add, change, or remove features,
        change or replace the third-party services we integrate with, and
        discontinue functionality. We do not promise that any particular feature
        will remain available. Where a change materially reduces the
        functionality of the paid service, we will give notice as described
        under <i>Changes to these terms</i>, and you may cancel.
      </p>
    ),
  },
  {
    heading: 'Communications',
    body: (
      <p>
        Some messages are part of providing the service, and you cannot opt out
        of them while you hold an account: magic-link sign-in emails, billing
        receipts and payment notices, security alerts, important account
        notices, and notices about changes to these Terms. Optional messages,
        such as job alerts, are covered by your notification settings and by the{' '}
        <PrivacyLink />.
      </p>
    ),
  },
  {
    heading: 'Privacy, data retention, and deletion',
    body: (
      <p>
        How we collect, use, share, retain, and delete personal information —
        including what happens to your profile, uploaded files, and generated
        documents when you delete your account — is described in our{' '}
        <PrivacyLink />. You can export your data at any time from your account
        settings before deleting it.
      </p>
    ),
  },
  {
    heading: 'Copyright complaints',
    body: (
      <>
        <p>
          If you believe material on WyrdFold infringes your copyright, send a
          notice to{' '}
          <a
            className='underline underline-offset-2'
            href={`mailto:${LEGAL_EMAIL}`}
          >
            {LEGAL_EMAIL}
          </a>{' '}
          including: your contact details; identification of the copyrighted
          work; identification of the material you say is infringing and where
          it is on the service; a statement that you have a good-faith belief
          the use is not authorized; a statement that the notice is accurate;
          and your signature (electronic is fine).
        </p>
        <p>
          We review notices and may remove material and terminate accounts of
          repeat infringers. If your material was removed and you believe that
          was a mistake, contact the same address.
        </p>
      </>
    ),
  },
  {
    heading: 'Suspension and termination',
    body: (
      <p>
        You may stop using WyrdFold and delete your account at any time — see{' '}
        <i>Subscriptions, billing, and cancellation</i> above for how that
        interacts with an active subscription. We may suspend or terminate
        access if you breach these Terms or to protect the service or other
        users. On termination, your right to use the service ends; sections that
        by their nature should survive — including Generated Content,
        Disclaimers, Limitation of liability, and Indemnification — remain in
        effect. What happens to your data afterwards is described in our{' '}
        <PrivacyLink />.
      </p>
    ),
  },
  {
    heading: 'Disclaimers',
    body: (
      <p>
        The service is provided “as is” and “as available,” without warranties
        of any kind, whether express or implied, including merchantability,
        fitness for a particular purpose, and non-infringement, to the fullest
        extent permitted by law. We do not guarantee that the service will be
        uninterrupted, timely, secure, or error-free — third-party AI providers,
        job boards, and infrastructure may be unavailable at times.
      </p>
    ),
  },
  {
    heading: 'Limitation of liability',
    body: (
      <>
        <p>
          To the fullest extent permitted by law, we will not be liable for any
          indirect, incidental, special, consequential, or punitive damages, or
          for lost opportunities, lost earnings, or employment outcomes. Our
          total liability for any claim relating to the service is limited to
          the greater of the amount you paid us in the prior 12 months or
          USD&nbsp;$100.
        </p>
        <p>
          Nothing in these Terms excludes or limits our liability to the extent
          that liability cannot lawfully be excluded or limited — including, in
          many jurisdictions, liability for fraud, fraudulent misrepresentation,
          death or personal injury caused by negligence, or rights you have as a
          consumer that cannot be waived.
        </p>
      </>
    ),
  },
  {
    heading: 'Indemnification',
    body: (
      <p>
        You agree to indemnify and hold harmless {LEGAL_ENTITY_NAME} from any
        third-party claim, demand, loss, or expense (including reasonable legal
        fees) arising out of: your use of the service in breach of these Terms
        or of applicable law; your infringement of another person’s intellectual
        property, privacy, or other rights; or content you supply that is
        fraudulent or that you know to be false or misleading.
        <b>
          {' '}
          Using WyrdFold as intended — including submitting a generated document
          to an employer — is not by itself a basis for indemnification.
        </b>{' '}
        We will notify you of any such claim, and you may not settle it in a way
        that imposes any obligation or admission on us without our prior written
        consent. This does not apply to the extent the claim arises from our own
        acts, omissions, or breach of these Terms.
      </p>
    ),
  },
  {
    heading: 'Dispute resolution',
    body: (
      <p>
        If you have a problem, contact us first at{' '}
        <a
          className='underline underline-offset-2'
          href={`mailto:${LEGAL_EMAIL}`}
        >
          {LEGAL_EMAIL}
        </a>{' '}
        — most issues are resolved that way, and we ask that you give us 30 days
        to try before starting formal proceedings. Any dispute that is not
        resolved informally will be brought exclusively in the state or federal
        courts located in California, and you and we consent to the personal
        jurisdiction of those courts. Either of us may bring a qualifying claim
        in small-claims court instead.
      </p>
    ),
  },
  {
    heading: 'Governing law',
    body: (
      <p>
        These Terms are governed by the laws of the State of California, without
        regard to its conflict-of-laws rules. If you reside in the European
        Union or the United Kingdom, this choice of law does not deprive you of
        the mandatory consumer protections afforded to you under the laws of
        your country of residence, and you may bring a claim in your local
        courts where such protections apply.
      </p>
    ),
  },
  {
    heading: 'Changes to these terms',
    body: (
      <p>
        We may update these Terms as the service evolves. Minor or
        non-substantive changes take effect when we post the updated version
        here, with a new effective date. For material changes, we will give you
        at least 30 days’ notice before they take effect — by email to your
        account address, by an in-product notice, or both. Continuing to use
        WyrdFold after a change takes effect means you accept the revised Terms;
        if you do not, cancel and stop using the service.
      </p>
    ),
  },
  {
    heading: 'General',
    body: (
      <ul className='ml-5 list-disc space-y-1'>
        <li>
          <b>Entire agreement.</b> These Terms are the entire agreement between
          you and us regarding the hosted service, and supersede any prior
          version of these Terms with respect to your use of it from the
          effective date onward. The <PrivacyLink /> is a statement of our data
          practices rather than a contractual term incorporated here, so that we
          can update it as privacy law requires without amending this agreement;
          it governs how we handle your personal information, and nothing in
          these Terms overrides it.
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
          <b>Assignment.</b> You may not assign these Terms. We may assign them
          in connection with a merger, acquisition, or sale of assets.
        </li>
        <li>
          <b>Force majeure.</b> We are not liable for any failure or delay
          caused by events beyond our reasonable control.
        </li>
      </ul>
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
        , or write to {LEGAL_ENTITY_NAME}, {LEGAL_ENTITY_ADDRESS}.
      </p>
    ),
  },
];

export default function TermsPage() {
  return (
    <LegalDocument
      title='Terms of Service'
      effective='August 20, 2026'
      intro={
        <p>
          These terms govern your use of the hosted WyrdFold service. Please
          read them together with our <PrivacyLink />.
        </p>
      }
      sections={SECTIONS}
    />
  );
}
