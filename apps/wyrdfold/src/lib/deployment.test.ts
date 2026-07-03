import { deploymentMode } from './deployment';

const ORIGINAL = process.env['NEXT_PUBLIC_DEPLOYMENT_MODE'];
afterEach(() => {
  if (ORIGINAL === undefined) {
    delete process.env['NEXT_PUBLIC_DEPLOYMENT_MODE'];
  } else {
    process.env['NEXT_PUBLIC_DEPLOYMENT_MODE'] = ORIGINAL;
  }
});

it('defaults to self_host when unset or empty', () => {
  delete process.env['NEXT_PUBLIC_DEPLOYMENT_MODE'];
  expect(deploymentMode()).toBe('self_host');
  process.env['NEXT_PUBLIC_DEPLOYMENT_MODE'] = '';
  expect(deploymentMode()).toBe('self_host');
});

it('returns the exact configured mode', () => {
  process.env['NEXT_PUBLIC_DEPLOYMENT_MODE'] = 'saas';
  expect(deploymentMode()).toBe('saas');
  process.env['NEXT_PUBLIC_DEPLOYMENT_MODE'] = 'self_host';
  expect(deploymentMode()).toBe('self_host');
});

it('throws loudly on a typo instead of silently flipping the homepage', () => {
  process.env['NEXT_PUBLIC_DEPLOYMENT_MODE'] = 'sass';
  expect(() => deploymentMode()).toThrow(/must be "self_host" or "saas"/);
  process.env['NEXT_PUBLIC_DEPLOYMENT_MODE'] = 'SAAS';
  expect(() => deploymentMode()).toThrow(/got "SAAS"/);
});
