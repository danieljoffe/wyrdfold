import type { UserTargetWithSummary } from '../../targets/types';
import { toActiveTargetTabs } from '../targetTabs';

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
