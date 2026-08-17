import { act, renderHook } from '@testing-library/react';
import { useTableSort } from '../useTableSort';

type Column = 'name' | 'date' | 'score';

describe('useTableSort', () => {
  it('initializes with the provided default column and desc order', () => {
    const { result } = renderHook(() => useTableSort<Column>('name'));

    expect(result.current.sort).toBe('name');
    expect(result.current.order).toBe('desc');
  });

  it('toggles order when the same column is sorted again', () => {
    const { result } = renderHook(() => useTableSort<Column>('name'));

    act(() => result.current.handleSort('name'));
    expect(result.current.order).toBe('asc');

    act(() => result.current.handleSort('name'));
    expect(result.current.order).toBe('desc');
  });

  it('switches column and resets order to desc when a different column is sorted', () => {
    const { result } = renderHook(() => useTableSort<Column>('name'));

    act(() => result.current.handleSort('name'));
    // flipped to asc
    expect(result.current.order).toBe('asc');

    act(() => result.current.handleSort('date'));
    expect(result.current.sort).toBe('date');
    expect(result.current.order).toBe('desc');
  });

  it('calls onSortChange each time handleSort fires', () => {
    const onSortChange = jest.fn();
    const { result } = renderHook(() =>
      useTableSort<Column>('name', onSortChange)
    );

    act(() => result.current.handleSort('name'));
    act(() => result.current.handleSort('date'));

    expect(onSortChange).toHaveBeenCalledTimes(2);
  });

  it('returns an arrow indicator only for the active column', () => {
    const { result } = renderHook(() => useTableSort<Column>('name'));

    expect(result.current.sortIndicator('name')).toBe(' ↓');
    expect(result.current.sortIndicator('date')).toBe('');

    act(() => result.current.handleSort('name'));
    expect(result.current.sortIndicator('name')).toBe(' ↑');
  });
});

// A column could only flip asc<->desc forever: once you sorted by Title there
// was no way back to the default ranking short of editing the URL. The cycle
// is now descending -> ascending -> cleared.
describe('useTableSort — clearing the sort', () => {
  it('cycles a non-default column through desc, asc, then back to the default', () => {
    const { result } = renderHook(() => useTableSort<Column>('score'));

    act(() => result.current.handleSort('name'));
    expect([result.current.sort, result.current.order]).toEqual(['name', 'desc']);

    act(() => result.current.handleSort('name'));
    expect([result.current.sort, result.current.order]).toEqual(['name', 'asc']);

    // Third click clears — back to the default ranking, not a third direction.
    act(() => result.current.handleSort('name'));
    expect([result.current.sort, result.current.order]).toEqual(['score', 'desc']);
  });

  it('reports reset only on the clearing click', () => {
    const onSortChange = jest.fn();
    const { result } = renderHook(() =>
      useTableSort<Column>('score', onSortChange)
    );

    act(() => result.current.handleSort('name'));
    act(() => result.current.handleSort('name'));
    expect(onSortChange).toHaveBeenLastCalledWith('name', 'asc', { reset: false });

    act(() => result.current.handleSort('name'));
    expect(onSortChange).toHaveBeenLastCalledWith('score', 'desc', { reset: true });
  });

  it('honours a non-desc default when clearing', () => {
    const onSortChange = jest.fn();
    const { result } = renderHook(() =>
      useTableSort<Column>('date', onSortChange, 'asc')
    );

    act(() => result.current.handleSort('name'));
    act(() => result.current.handleSort('name'));
    act(() => result.current.handleSort('name'));
    expect([result.current.sort, result.current.order]).toEqual(['date', 'asc']);
  });

  it('reads as a two-state toggle on the default column, which is correct', () => {
    // "No sort" for the default column IS the default, so there is no third
    // visible state to offer there.
    const { result } = renderHook(() => useTableSort<Column>('score'));

    act(() => result.current.handleSort('score'));
    expect(result.current.order).toBe('asc');
    act(() => result.current.handleSort('score'));
    expect([result.current.sort, result.current.order]).toEqual(['score', 'desc']);
  });

  it('announces what the next click will do', () => {
    const { result } = renderHook(() => useTableSort<Column>('score'));

    expect(result.current.nextSortAction('name')).toBe('descending');
    act(() => result.current.handleSort('name'));
    expect(result.current.nextSortAction('name')).toBe('ascending');
    act(() => result.current.handleSort('name'));
    expect(result.current.nextSortAction('name')).toBe('clear');
  });
});
