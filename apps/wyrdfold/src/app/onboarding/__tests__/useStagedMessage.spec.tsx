import { renderHook, act } from '@testing-library/react';
import { useStagedMessage } from '../useStagedMessage';

const STAGES = ['Uploading...', 'Still parsing...', 'Almost there...'] as const;

describe('useStagedMessage', () => {
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
    jest.useRealTimers();
  });

  it('returns the first message immediately and advances on the interval', () => {
    const { result } = renderHook(() => useStagedMessage(STAGES, true, 8000));
    expect(result.current).toBe('Uploading...');

    act(() => {
      jest.advanceTimersByTime(8000);
    });
    expect(result.current).toBe('Still parsing...');

    act(() => {
      jest.advanceTimersByTime(8000);
    });
    expect(result.current).toBe('Almost there...');
  });

  it('holds on the last message instead of wrapping', () => {
    const { result } = renderHook(() => useStagedMessage(STAGES, true, 8000));
    act(() => {
      jest.advanceTimersByTime(8000 * 10);
    });
    expect(result.current).toBe('Almost there...');
  });

  it('does not advance while inactive, and resets after a run ends', () => {
    const { result, rerender } = renderHook(
      ({ active }: { active: boolean }) =>
        useStagedMessage(STAGES, active, 8000),
      { initialProps: { active: false } }
    );
    act(() => {
      jest.advanceTimersByTime(8000 * 3);
    });
    expect(result.current).toBe('Uploading...');

    // A run advances…
    rerender({ active: true });
    act(() => {
      jest.advanceTimersByTime(8000);
    });
    expect(result.current).toBe('Still parsing...');

    // …and ending the run resets to the first message for the next one.
    rerender({ active: false });
    expect(result.current).toBe('Uploading...');
  });

  it('never advances a single-message list', () => {
    const { result } = renderHook(() =>
      useStagedMessage(['Only...'], true, 10)
    );
    act(() => {
      jest.advanceTimersByTime(1000);
    });
    expect(result.current).toBe('Only...');
  });
});
