import { renderHook } from '@testing-library/react';
import { useFocusTrap } from '../useFocusTrap';

describe('useFocusTrap', () => {
  let container: HTMLDivElement;

  beforeEach(() => {
    container = document.createElement('div');
    document.body.appendChild(container);
  });

  afterEach(() => {
    document.body.removeChild(container);
  });

  it('returns a ref object', () => {
    const { result } = renderHook(() => useFocusTrap(false));

    expect(result.current).toBeDefined();
    expect(result.current).toHaveProperty('current');
  });

  it('does not focus anything when inactive', () => {
    container.innerHTML = '<button>First</button><button>Last</button>';
    const firstButton = container.querySelector('button') as HTMLButtonElement;
    const focusSpy = jest.spyOn(firstButton, 'focus');

    const { result } = renderHook(() => useFocusTrap(false));

    // Manually assign the container to the ref
    Object.defineProperty(result.current, 'current', {
      writable: true,
      value: container,
    });

    expect(focusSpy).not.toHaveBeenCalled();

    focusSpy.mockRestore();
  });

  it('focuses the first focusable element when activated', () => {
    container.innerHTML =
      '<button data-testid="first">First</button>' +
      '<input type="text" />' +
      '<button data-testid="last">Last</button>';

    const firstButton = container.querySelector(
      '[data-testid="first"]'
    ) as HTMLButtonElement;

    const { result, rerender } = renderHook(
      ({ isActive }) => useFocusTrap(isActive),
      { initialProps: { isActive: false } }
    );

    // Assign the container to the ref before activating
    Object.defineProperty(result.current, 'current', {
      writable: true,
      value: container,
    });

    rerender({ isActive: true });

    expect(document.activeElement).toBe(firstButton);
  });

  it('wraps focus from last element to first on Tab', () => {
    container.innerHTML =
      '<button data-testid="first">First</button>' +
      '<button data-testid="last">Last</button>';

    const firstButton = container.querySelector(
      '[data-testid="first"]'
    ) as HTMLButtonElement;
    const lastButton = container.querySelector(
      '[data-testid="last"]'
    ) as HTMLButtonElement;

    const { result, rerender } = renderHook(
      ({ isActive }) => useFocusTrap(isActive),
      { initialProps: { isActive: false } }
    );

    Object.defineProperty(result.current, 'current', {
      writable: true,
      value: container,
    });

    rerender({ isActive: true });

    // Focus is on the first element after activation; move to last
    lastButton.focus();
    expect(document.activeElement).toBe(lastButton);

    // Press Tab on the last element
    const tabEvent = new KeyboardEvent('keydown', {
      key: 'Tab',
      bubbles: true,
      cancelable: true,
    });
    document.dispatchEvent(tabEvent);

    expect(document.activeElement).toBe(firstButton);
  });

  it('wraps focus from first element to last on Shift+Tab', () => {
    container.innerHTML =
      '<button data-testid="first">First</button>' +
      '<button data-testid="last">Last</button>';

    const firstButton = container.querySelector(
      '[data-testid="first"]'
    ) as HTMLButtonElement;
    const lastButton = container.querySelector(
      '[data-testid="last"]'
    ) as HTMLButtonElement;

    const { result, rerender } = renderHook(
      ({ isActive }) => useFocusTrap(isActive),
      { initialProps: { isActive: false } }
    );

    Object.defineProperty(result.current, 'current', {
      writable: true,
      value: container,
    });

    rerender({ isActive: true });

    // Focus should be on first element after activation
    expect(document.activeElement).toBe(firstButton);

    // Press Shift+Tab on the first element
    const shiftTabEvent = new KeyboardEvent('keydown', {
      key: 'Tab',
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    });
    document.dispatchEvent(shiftTabEvent);

    expect(document.activeElement).toBe(lastButton);
  });

  // #196: focus must return to the trigger when the trap closes — by ANY path
  // (the old code only attempted it on Escape, via an attribute nothing set).
  it('restores focus to the previously-focused element on close', () => {
    container.innerHTML =
      '<button data-testid="first">First</button>' +
      '<button data-testid="last">Last</button>';

    // The trigger holds focus before the trap opens.
    const trigger = document.createElement('button');
    trigger.textContent = 'Trigger';
    document.body.appendChild(trigger);
    trigger.focus();
    expect(document.activeElement).toBe(trigger);

    const { result, rerender } = renderHook(
      ({ isActive }) => useFocusTrap(isActive),
      { initialProps: { isActive: false } }
    );
    Object.defineProperty(result.current, 'current', {
      writable: true,
      value: container,
    });

    // Open → focus moves into the trap.
    rerender({ isActive: true });
    expect(document.activeElement).toBe(
      container.querySelector('[data-testid="first"]')
    );

    // Close (isActive flips false) → focus returns to the trigger.
    rerender({ isActive: false });
    expect(document.activeElement).toBe(trigger);

    document.body.removeChild(trigger);
  });

  // #196: the focusable list is re-queried each Tab, so elements added after
  // open (async modal content) are trapped too.
  it('includes focusable elements added after activation when wrapping Tab', () => {
    container.innerHTML = '<button data-testid="first">First</button>';

    const { result, rerender } = renderHook(
      ({ isActive }) => useFocusTrap(isActive),
      { initialProps: { isActive: false } }
    );
    Object.defineProperty(result.current, 'current', {
      writable: true,
      value: container,
    });
    rerender({ isActive: true });

    // Add a second focusable AFTER activation.
    const late = document.createElement('button');
    late.setAttribute('data-testid', 'late');
    container.appendChild(late);

    const first = container.querySelector(
      '[data-testid="first"]'
    ) as HTMLButtonElement;

    // Tab from the newly-added last element wraps to first — only works if the
    // list is re-queried (the old code captured it once at open, so `late`
    // wasn't the known "last" and Tab wouldn't wrap).
    late.focus();
    document.dispatchEvent(
      new KeyboardEvent('keydown', {
        key: 'Tab',
        bubbles: true,
        cancelable: true,
      })
    );
    expect(document.activeElement).toBe(first);
  });

  it('does nothing when there are no focusable elements', () => {
    container.innerHTML = '<div>No focusable elements here</div>';

    const { result, rerender } = renderHook(
      ({ isActive }) => useFocusTrap(isActive),
      { initialProps: { isActive: false } }
    );

    Object.defineProperty(result.current, 'current', {
      writable: true,
      value: container,
    });

    // Should not throw
    rerender({ isActive: true });

    expect(document.activeElement).not.toBe(container);
  });

  it('removes keydown event listeners on cleanup', () => {
    container.innerHTML = '<button>First</button><button>Last</button>';

    const removeSpy = jest.spyOn(document, 'removeEventListener');

    const { result, rerender, unmount } = renderHook(
      ({ isActive }) => useFocusTrap(isActive),
      { initialProps: { isActive: false } }
    );

    Object.defineProperty(result.current, 'current', {
      writable: true,
      value: container,
    });

    rerender({ isActive: true });

    unmount();

    const keydownRemovals = removeSpy.mock.calls.filter(
      call => call[0] === 'keydown'
    );
    // One listener now: handleTabKey. Escape-to-close lives in the consumer;
    // focus restoration moved to the effect cleanup (runs on every close path).
    expect(keydownRemovals).toHaveLength(1);

    removeSpy.mockRestore();
  });
});
