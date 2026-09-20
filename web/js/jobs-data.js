import { api } from './api.js';

let pending;

// All views share concurrent reads; rejected reads never poison the next request.
export function getJobs() {
  if (!pending) pending = api('jobs').finally(() => { pending = null; });
  return pending;
}
