import type { ReactNode } from 'react';

export interface LegalSection {
  heading: string;
  body: ReactNode;
}

interface LegalDocumentProps {
  title: string;
  /** Human-readable effective date, or a draft marker until legal sign-off. */
  effective: string;
  intro: ReactNode;
  sections: LegalSection[];
}

/**
 * Shared shell for the public legal pages (Terms, Privacy). Renders inside the
 * (public) marketing layout, so it inherits the header, beta strip, and footer.
 *
 * NOTE: the content passed in is an UNREVIEWED SCAFFOLD. Bracketed placeholders
 * (e.g. `[Legal Entity Name]`) and the draft banner below must be completed by
 * counsel before these pages are relied on. The `signup_mode=open` launch gate
 * depends on that review.
 */
export default function LegalDocument({
  title,
  effective,
  intro,
  sections,
}: LegalDocumentProps) {
  return (
    <article className='mx-auto w-full max-w-3xl px-4 py-12 md:px-6 md:py-16'>
      <header>
        <h1 className='text-3xl font-bold tracking-tight text-text-primary md:text-4xl'>
          {title}
        </h1>
        <p className='mt-3 text-sm text-text-tertiary'>
          Effective date: {effective}
        </p>
      </header>

      {/* Draft notice — remove once counsel has reviewed and the bracketed
          placeholders are filled in. Kept visible on purpose so an
          accidental early publish is obvious rather than silent. */}
      <div
        role='note'
        className='mt-6 rounded-lg border border-warning/40 bg-warning/10 px-4 py-3 text-sm text-text-secondary'
      >
        <span className='font-semibold text-text-primary'>
          Draft — pending legal review.
        </span>{' '}
        This is a starting template, not legal advice. Every{' '}
        <span className='font-mono text-xs'>[bracketed]</span> placeholder must
        be completed and this notice removed before publishing.
      </div>

      <div className='mt-8 space-y-4 text-base leading-relaxed text-text-secondary'>
        {intro}
      </div>

      <div className='mt-10 space-y-9'>
        {sections.map((section, index) => (
          <section
            key={section.heading}
            aria-labelledby={slugify(section.heading)}
          >
            <h2
              id={slugify(section.heading)}
              className='text-lg font-semibold text-text-primary'
            >
              <span className='mr-2 font-mono text-sm text-text-tertiary tabular-nums'>
                {String(index + 1).padStart(2, '0')}
              </span>
              {section.heading}
            </h2>
            <div className='mt-2.5 space-y-3 text-[15px] leading-relaxed text-text-secondary'>
              {section.body}
            </div>
          </section>
        ))}
      </div>
    </article>
  );
}

function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/(^-|-$)/g, '');
}
