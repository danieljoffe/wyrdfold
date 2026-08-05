import Button from '@/components/kit/Button';

interface JobsLoadErrorProps {
  onRetry: () => void;
}

/**
 * Failed-load state for the jobs list (#604). Rendered instead of the
 * "No jobs found" empty state when the fetch itself failed — a backend
 * hiccup must not read as "you have no matches" (during the 2026-08-05
 * drive a transient /api/jobs failure left exactly that impression).
 */
export default function JobsLoadError({ onRetry }: JobsLoadErrorProps) {
  return (
    <div
      role='alert'
      className='flex flex-col items-center gap-3 py-16 text-center'
    >
      <p className='text-sm text-text-secondary'>
        Jobs couldn&rsquo;t be loaded just now. This is a loading problem, not
        an empty result — your matches are still there.
      </p>
      <Button name='jobs-load-retry' variant='outline' onClick={onRetry}>
        Retry
      </Button>
    </div>
  );
}
