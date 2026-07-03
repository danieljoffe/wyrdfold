// Full-page navigation to an EXTERNAL url (hosted Stripe Checkout/Portal —
// places the app router can't go). A module so component specs can mock it:
// jsdom's window.location is non-configurable and its assign() throws.
export function navigateTo(url: string): void {
  window.location.assign(url);
}
