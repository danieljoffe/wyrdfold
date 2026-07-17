import { bffSecretHeader } from './bffSecret';

describe('bffSecretHeader', () => {
  const original = process.env['WYRDFOLD_BFF_SECRET'];
  afterEach(() => {
    if (original === undefined) delete process.env['WYRDFOLD_BFF_SECRET'];
    else process.env['WYRDFOLD_BFF_SECRET'] = original;
  });

  it('returns the x-wyrdfold-bff header when the secret is set', () => {
    process.env['WYRDFOLD_BFF_SECRET'] = 'sekret';
    expect(bffSecretHeader()).toEqual({ 'x-wyrdfold-bff': 'sekret' });
  });

  it('returns an empty object when the secret is unset (fail-open rollout)', () => {
    delete process.env['WYRDFOLD_BFF_SECRET'];
    expect(bffSecretHeader()).toEqual({});
  });
});
