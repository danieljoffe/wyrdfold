import type { NextRequest } from 'next/server';

import {
  LLM_TIMEOUT_MS,
  proxyToWyrdfoldAPI,
  readJsonBody,
} from '@/lib/api/proxy';

type Params = { params: Promise<{ id: string }> };

/**
 * Activation, optionally as a SWAP.
 *
 * The body is `{ deactivate_target_id }` when the user picked a target to
 * deactivate to free a slot under the active-target cap, and absent otherwise.
 *
 * This route used to ignore the request entirely (`_request`, no body
 * forwarded), which is fine while activation takes no input — and silently
 * wrong the moment it does. The upstream would have received an empty body,
 * treated it as a plain activation, and returned the same 409 the swap exists
 * to resolve, with nothing in the logs to say why. Same shape as the
 * salary-filter drop this BFF layer produced before.
 *
 * An absent/empty body stays valid: the upstream model is fully optional, so
 * every existing caller is unaffected.
 */
export async function POST(request: NextRequest, { params }: Params) {
  const { id } = await params;

  // Callers that send nothing at all are the common case (plain activate), and
  // `readJsonBody` treats an empty body as a parse failure — so only forward a
  // body when one was actually sent.
  const hasBody = (request.headers.get('content-type') ?? '').includes(
    'application/json'
  );
  if (!hasBody) {
    return proxyToWyrdfoldAPI(`/targets/${id}/activate`, {
      method: 'POST',
      timeoutMs: LLM_TIMEOUT_MS,
    });
  }

  const parsed = await readJsonBody(request);
  if (!parsed.ok) return parsed.response;
  return proxyToWyrdfoldAPI(`/targets/${id}/activate`, {
    method: 'POST',
    body: parsed.body,
    timeoutMs: LLM_TIMEOUT_MS,
  });
}
