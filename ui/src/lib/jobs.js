import { useQuery } from '@tanstack/react-query';
import { jobsQuery } from './queries.js';
import { useStreamDown } from './events.js';

// The job queue, kept live by the event stream; polls every 30 s only while
// the stream is down.
export function useJobs() {
  const down = useStreamDown();
  return useQuery({ ...jobsQuery, refetchInterval: down ? 30_000 : false });
}

export function jobStatusClass(status) {
  return status === 'running' ? 'tag-accent'
    : status === 'failed' ? 'tag-danger'
      : status === 'planned' ? 'tag-outline' : 'tag-neutral'; // cancelled stays grey
}
