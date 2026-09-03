import type { ReactNode } from 'react';
import { Card, CardContent } from '@danieljoffe/shared-ui/Card';
import { Heading } from '@danieljoffe/shared-ui/Heading';
import { Text } from '@danieljoffe/shared-ui/Text';

/**
 * The centered full-page status card (not-found / error surfaces).
 *
 * Extracted after the FOURTH hand-rolled copy shipped (#971): the root
 * not-found, (app)/not-found, (app)/error and search/not-found each carried
 * the same Card → CardContent → Heading → Text → actions composition, and
 * the copies had already drifted (max-w-sm vs max-w-md bodies; heading
 * inside vs outside the card). Canonical form: heading INSIDE the card
 * (the card describes itself), max-w-md body, actions row below.
 *
 * Call sites keep their own OUTER shell — sidebar wrapper, vertical
 * centering, plain column — that is the part that legitimately differs.
 * No 'use client': stateless, so server components render it directly and
 * client components (the error boundary) pull it into their bundle.
 */
export default function StatusCard({
  title,
  body,
  actions,
}: {
  title: string;
  body: ReactNode;
  /** One or more Button/LinkButton elements; rendered in a horizontal row. */
  actions: ReactNode;
}) {
  return (
    <Card className='max-w-md w-full'>
      <CardContent className='flex flex-col items-center gap-4 py-12 text-center'>
        <Heading variant='hero' as='h1'>
          {title}
        </Heading>
        <Text variant='body' as='p' className='max-w-md'>
          {body}
        </Text>
        <div className='flex gap-2'>{actions}</div>
      </CardContent>
    </Card>
  );
}
