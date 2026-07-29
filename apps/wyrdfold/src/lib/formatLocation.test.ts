import { formatLocation } from './formatLocation';

describe('formatLocation', () => {
  it('composes City, ST, Country', () => {
    expect(
      formatLocation({ city: 'San Francisco', state: 'CA', country: 'US' })
    ).toBe('San Francisco, CA, US');
  });

  it('omits missing parts (non-US city without state)', () => {
    expect(formatLocation({ city: 'London', country: 'UK' })).toBe(
      'London, UK'
    );
  });

  it('renders country-only rows', () => {
    expect(formatLocation({ country: 'US' })).toBe('US');
  });

  it('renders remote-only as Remote', () => {
    expect(formatLocation({ location_remote: true, location: 'Remote' })).toBe(
      'Remote'
    );
  });

  it('renders remote + country as a qualifier', () => {
    expect(formatLocation({ location_remote: true, country: 'US' })).toBe(
      'Remote (US)'
    );
  });

  it('renders remote + hub city in full', () => {
    expect(
      formatLocation({
        location_remote: true,
        city: 'Austin',
        state: 'TX',
        country: 'US',
      })
    ).toBe('Remote — Austin, TX, US');
  });

  it('falls back to the raw string when nothing parsed', () => {
    expect(formatLocation({ location: '2 Locations' })).toBe('2 Locations');
    expect(formatLocation({ location: 'Hybrid' })).toBe('Hybrid');
  });

  it('returns empty string with no signal at all', () => {
    expect(formatLocation({})).toBe('');
    expect(formatLocation({ location: null })).toBe('');
  });

  it('ignores a stale raw string once parts exist', () => {
    expect(
      formatLocation({
        location: 'Remote - United States',
        country: 'US',
        location_remote: true,
      })
    ).toBe('Remote (US)');
  });
});
