import { useEffect, useState } from 'react';

/** What the NEXT click on a column will do — drives the button's label. */
export type NextSortAction = 'descending' | 'ascending' | 'clear';

export function useTableSort<T extends string>(
  defaultSort: T,
  onSortChange?: (
    sort: T,
    order: 'asc' | 'desc',
    meta: { reset: boolean }
  ) => void,
  defaultOrder: 'asc' | 'desc' = 'desc',
  /** External source of truth (e.g. URL query params). When provided and
   *  it changes, the hook re-initialises ``sort``/``order`` from it. Used
   *  to wire browser back/forward to the table state. */
  controlled?: { sort: T; order: 'asc' | 'desc' }
) {
  const [sort, setSort] = useState<T>(controlled?.sort ?? defaultSort);
  const [order, setOrder] = useState<'asc' | 'desc'>(
    controlled?.order ?? defaultOrder
  );

  // Re-sync from the controlled value whenever it changes (e.g. user hit
  // browser back, URL flipped from ``sort=score`` to ``sort=title``).
  useEffect(() => {
    if (controlled) {
      setSort(controlled.sort);
      setOrder(controlled.order);
    }
  }, [controlled?.sort, controlled?.order, controlled]);

  /**
   * Cycle a column: descending → ascending → cleared.
   *
   * The third state was missing entirely — a column could only flip between
   * asc and desc forever, so once you sorted by Title there was no way back to
   * the default ranking short of editing the URL. "Cleared" means exactly
   * that: fall back to ``defaultSort``/``defaultOrder``, which for the jobs
   * list is score-descending, i.e. best matches first.
   *
   * Clicking the default column therefore reads as a two-state toggle, which
   * is correct — "no sort" for that column IS the default.
   */
  function handleSort(column: T) {
    let nextSort: T;
    let nextOrder: 'asc' | 'desc';
    let reset = false;

    if (sort !== column) {
      nextSort = column;
      nextOrder = 'desc';
    } else if (order === 'desc') {
      nextSort = column;
      nextOrder = 'asc';
    } else {
      nextSort = defaultSort;
      nextOrder = defaultOrder;
      reset = true;
    }

    setSort(nextSort);
    setOrder(nextOrder);
    onSortChange?.(nextSort, nextOrder, { reset });
  }

  function sortIndicator(col: T) {
    return sort === col ? (order === 'asc' ? ' ↑' : ' ↓') : '';
  }

  /**
   * What clicking ``col`` will do next. A static "Sort by Title" label hides
   * the cycle — with three states the control has to say where it's going, or
   * a screen-reader user has no way to know a third click clears it.
   */
  function nextSortAction(col: T): NextSortAction {
    if (sort !== col) return 'descending';
    return order === 'desc' ? 'ascending' : 'clear';
  }

  return {
    sort,
    order,
    handleSort,
    sortIndicator,
    nextSortAction,
  } as const;
}
