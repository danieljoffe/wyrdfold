/**
 * The `@modal` slot's resting state (#467 §11.2 fast-follow): nothing. Renders
 * whenever the slot has no intercepted match — on `/search` itself, and on any
 * HARD load of `/search/[id]` (where interception doesn't apply and the full
 * page in `children` owns the detail instead).
 */
export default function SearchModalDefault() {
  return null;
}
