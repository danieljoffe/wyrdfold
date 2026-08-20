/**
 * The legal pages name the operating entity and a physical address. This repo
 * is PUBLIC, so those values come from the environment and never enter git
 * history. These pin the two properties that make that safe.
 */

const NAME_VAR = 'LEGAL_ENTITY_NAME';
const ADDRESS_VAR = 'LEGAL_ENTITY_ADDRESS';

async function loadWith(
  env: Partial<Record<string, string | undefined>>
): Promise<typeof import('../legalEntity')> {
  jest.resetModules();
  const previous = { ...process.env };
  for (const [key, value] of Object.entries(env)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  try {
    return await import('../legalEntity');
  } finally {
    process.env = previous;
  }
}

describe('legalEntity', () => {
  it('renders the configured entity and address', async () => {
    const mod = await loadWith({
      [NAME_VAR]: 'Jane Doe',
      [ADDRESS_VAR]: '1 Example St, Springfield, CA 90000',
    });

    expect(mod.LEGAL_ENTITY_NAME).toBe('Jane Doe');
    expect(mod.LEGAL_ENTITY_ADDRESS).toBe(
      '1 Example St, Springfield, CA 90000'
    );
    expect(mod.LEGAL_ENTITY_IS_PLACEHOLDER).toBe(false);
  });

  it('falls back to a VISIBLE placeholder when unset', async () => {
    // The failure that matters: an empty string renders "operated by , " —
    // a grammatical sentence that reads as finished, so an unset variable
    // would ship silently. The placeholder must be unmistakable instead.
    const mod = await loadWith({
      [NAME_VAR]: undefined,
      [ADDRESS_VAR]: undefined,
    });

    expect(mod.LEGAL_ENTITY_NAME).toBe('[Legal Entity Name]');
    expect(mod.LEGAL_ENTITY_ADDRESS).toBe('[registered address]');
    expect(mod.LEGAL_ENTITY_IS_PLACEHOLDER).toBe(true);
  });

  it('treats blank and whitespace-only values as unset', async () => {
    // A var set to "" or " " in a deploy config is the likeliest real-world
    // mistake, and is exactly the case that would otherwise render an empty
    // gap rather than an obvious placeholder.
    const mod = await loadWith({ [NAME_VAR]: '   ', [ADDRESS_VAR]: '' });

    expect(mod.LEGAL_ENTITY_NAME).toBe('[Legal Entity Name]');
    expect(mod.LEGAL_ENTITY_ADDRESS).toBe('[registered address]');
    expect(mod.LEGAL_ENTITY_IS_PLACEHOLDER).toBe(true);
  });

  it('flags a partially-configured deploy', async () => {
    const mod = await loadWith({
      [NAME_VAR]: 'Jane Doe',
      [ADDRESS_VAR]: undefined,
    });

    expect(mod.LEGAL_ENTITY_NAME).toBe('Jane Doe');
    expect(mod.LEGAL_ENTITY_IS_PLACEHOLDER).toBe(true);
  });
});
