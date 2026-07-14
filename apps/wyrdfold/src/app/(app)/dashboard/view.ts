/** Home's two sections (UX/IA Fork A): the daily launcher and the
 *  historical trends view (the former /insights page). */
export type HomeView = 'today' | 'trends';

export function parseHomeView(raw: string | string[] | undefined): HomeView {
  return raw === 'trends' ? 'trends' : 'today';
}
