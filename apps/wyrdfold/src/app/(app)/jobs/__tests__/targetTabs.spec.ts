import type { UserTargetWithSummary } from '../../targets/types';
import { resolveRequestedTarget, toActiveTargetTabs } from '../targetTabs';

// The helper reads only user_target.is_active, target.id and target.label;
// build just enough of the shape and cast.
function mk(
  id: string,
  label: string,
  is_active: boolean
): UserTargetWithSummary {
  return {
    user_target: { id: `ut-${id}`, target_id: id, is_active },
    target: { id, label },
  } as unknown as UserTargetWithSummary;
}

describe('toActiveTargetTabs', () => {
  it('omits paused (inactive) memberships', () => {
    const tabs = toActiveTargetTabs([
      mk('t1', 'Frontend', true),
      mk('t2', 'Backend', false), // paused → dropped
      mk('t3', 'Data', true),
    ]);
    expect(tabs).toEqual([
      { id: 't1', label: 'Frontend' },
      { id: 't3', label: 'Data' },
    ]);
  });

  it('returns [] when every membership is paused', () => {
    expect(toActiveTargetTabs([mk('t1', 'Frontend', false)])).toEqual([]);
  });

  it('returns [] for no memberships', () => {
    expect(toActiveTargetTabs([])).toEqual([]);
  });

  it('carries only id + label through (no extra fields leak into the tab)', () => {
    const [tab] = toActiveTargetTabs([mk('t1', 'Frontend', true)]);
    expect(Object.keys(tab).sort()).toEqual(['id', 'label']);
  });
});

describe('resolveRequestedTarget', () => {
  const targets = [
    mk('t1', 'Frontend', true),
    mk('t2', 'Backend', false), // paused
  ];

  it('is ok when no target is requested', () => {
    expect(resolveRequestedTarget(undefined, targets)).toEqual({ kind: 'ok' });
  });

  it('is ok for an ACTIVE membership', () => {
    expect(resolveRequestedTarget('t1', targets)).toEqual({ kind: 'ok' });
  });

  it('redirects for an id the caller has no membership for', () => {
    // Keeps the pre-existing guard: never confirm an id that isn't theirs.
    expect(resolveRequestedTarget('nope', targets)).toEqual({
      kind: 'redirect',
    });
  });

  it('reports PAUSED (not redirect) for a real but deactivated membership', () => {
    // The regression this whole change exists for: 't2' used to fall into the
    // same silent redirect as an unknown id, dumping the user on whichever
    // other target happened to be active with no explanation.
    expect(resolveRequestedTarget('t2', targets)).toEqual({
      kind: 'paused',
      target: { id: 't2', label: 'Backend' },
    });
  });

  it('carries the label through so the notice can name the target', () => {
    const res = resolveRequestedTarget('t2', targets);
    expect(res.kind).toBe('paused');
    if (res.kind !== 'paused') throw new Error('expected paused');
    expect(res.target.label).toBe('Backend');
  });

  it('redirects when the user has no memberships at all', () => {
    expect(resolveRequestedTarget('t1', [])).toEqual({ kind: 'redirect' });
  });
});
