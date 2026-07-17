import { NextResponse } from 'next/server';

import {
  LLM_TIMEOUT_MS,
  proxyToWyrdfoldAPI,
  readJsonBody,
} from '@/lib/api/proxy';

/**
 * Create-or-link a target from an AI search-suggestion the user picked (the
 * completion of the catalog-search LLM fallback). Forwards {label, description}
 * to `POST /targets/from-suggestion`, which dedups against the shared catalog
 * server-side and links `is_active=False`. Distinct from `/from-manual`: no
 * experience profile required, and the label is already canonical so the API
 * skips the inline normalization LLM call. Still LLM-backed (deferred profile
 * derivation) → long timeout.
 */
interface FromSuggestionBody {
  label?: unknown;
  description?: unknown;
}

const MAX_LABEL_LENGTH = 200;
const MAX_DESCRIPTION_LENGTH = 500;

export async function POST(request: Request) {
  const parsed = await readJsonBody<FromSuggestionBody>(request);
  if (!parsed.ok) return parsed.response;

  const label =
    typeof parsed.body.label === 'string' ? parsed.body.label.trim() : '';
  if (!label || label.length > MAX_LABEL_LENGTH) {
    return NextResponse.json(
      { error: 'A role title of up to 200 characters is required.' },
      { status: 400 }
    );
  }

  const description =
    typeof parsed.body.description === 'string'
      ? parsed.body.description.trim().slice(0, MAX_DESCRIPTION_LENGTH)
      : undefined;

  return proxyToWyrdfoldAPI('/targets/from-suggestion', {
    method: 'POST',
    body: { label, description },
    timeoutMs: LLM_TIMEOUT_MS,
  });
}
