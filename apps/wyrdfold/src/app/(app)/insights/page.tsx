import { redirect } from 'next/navigation';

/**
 * /insights merged into Home as its "Trends" section (UX/IA Fork A) —
 * this route survives only so bookmarks and old links keep working.
 */
export default function FittedInsights() {
  redirect('/dashboard?view=trends');
}
