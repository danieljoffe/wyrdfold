import StatusCard from '@/components/StatusCard';
import LinkButton from '@/components/kit/LinkButton';

export default function AppNotFound() {
  return (
    <div className='flex min-h-full items-center justify-center py-12'>
      <StatusCard
        title='Page not found'
        body={
          "We couldn't find that page. It may have been moved or the URL is incorrect."
        }
        actions={
          <LinkButton
            name='wyrdfold-not-found-home'
            variant='primary'
            size='sm'
            href='/dashboard'
          >
            Back to dashboard
          </LinkButton>
        }
      />
    </div>
  );
}
