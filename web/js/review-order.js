// How the review list is ordered. `likely` puts the lines most likely to need
// a listen first (each row's review_priority, from doblarr.decisions); lines
// that tie keep their place on the timeline. `timeline` is the episode order.

export const ORDERS = [
  ['likely', 'Likeliest problems first'],
  ['timeline', 'Timeline'],
];

const start = row => row.source_start ?? row.start ?? 0;

export function orderRows(rows, order) {
  if (order !== 'likely') return [...rows];
  return [...rows].sort((a, b) =>
    (b.review_priority?.score || 0) - (a.review_priority?.score || 0) || start(a) - start(b));
}

// The order a review opens in: likeliest first when the run scored its lines.
export function defaultOrder(data) {
  return data?.settings?.review_order && data.segments?.some(s => s.review_priority?.score)
    ? 'likely' : 'timeline';
}
