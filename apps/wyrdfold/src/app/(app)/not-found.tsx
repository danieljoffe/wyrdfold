import { Card, CardContent } from '@danieljoffe/shared-ui/Card';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Text } from '@danieljoffe/shared-ui/Text';
import LinkButton from '@/components/kit/LinkButton';

export default function AppNotFound() {
  return (
    <div className='flex flex-col gap-6'>
      <Heading variant='hero' as='h1'>
        Page not found
      </Heading>
      <Card>
        <CardContent className='flex flex-col items-center gap-4 py-12 text-center'>
          <Text variant='body' as='p' className='max-w-md'>
            We couldn&apos;t find that page. It may have been moved or the URL
            is incorrect.
          </Text>
          <LinkButton
            name='wyrdfold-not-found-home'
            variant='primary'
            size='sm'
            href='/dashboard'
          >
            Back to dashboard
          </LinkButton>
        </CardContent>
      </Card>
    </div>
  );
}
