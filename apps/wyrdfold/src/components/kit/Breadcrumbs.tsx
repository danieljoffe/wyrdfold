'use client';

import type { MouseEvent } from 'react';
import { useRouter } from 'next/navigation';
import { Breadcrumb } from '@danieljoffe/shared-ui/Breadcrumb';
import type { BreadcrumbItem } from '@danieljoffe/shared-ui/Breadcrumb';

export type { BreadcrumbItem };

/** Cap a dynamic crumb (job title, target label) so the trail stays one
 *  line — some scraped titles run to 100+ chars (bilingual postings). */
export function crumbLabel(label: string, max = 60): string {
  return label.length > max ? `${label.slice(0, max - 1)}…` : label;
}

/**
 * shared-ui Breadcrumb with Next.js client navigation. The library renders
 * plain `<a href>` (it's framework-agnostic), which in an app-router app
 * means a full document reload per crumb click. This kit wrapper keeps the
 * library's exact rendering (nav aria-label, aria-current, chevrons) and
 * adds client nav by event delegation: internal-href clicks are intercepted
 * into `router.push`. Modified clicks (cmd/ctrl/shift/alt, middle button)
 * pass through so open-in-new-tab still works.
 *
 * Upstream candidate: a `linkComponent` prop on the library Breadcrumb would
 * make this wrapper a pure re-export.
 */
export default function Breadcrumbs({
  items,
  className,
}: {
  items: BreadcrumbItem[];
  className?: string;
}) {
  const router = useRouter();

  function handleClick(e: MouseEvent<HTMLDivElement>) {
    if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) {
      return;
    }
    const anchor = (e.target as HTMLElement).closest('a');
    const href = anchor?.getAttribute('href');
    if (href?.startsWith('/')) {
      e.preventDefault();
      router.push(href);
    }
  }

  return (
    <div onClick={handleClick} className='w-fit'>
      {/* Conditional spread keeps exactOptionalPropertyTypes happy — the lib
          declares `className?: string`, not `string | undefined`. */}
      <Breadcrumb items={items} {...(className ? { className } : {})} />
    </div>
  );
}
