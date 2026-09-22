import { apiUrl } from './api.js';
import { safeGet } from './dom.js';

// One audio element for the whole review, deliberately.
//
// A/B is only useful if switching keeps your place in the exchange, and two
// <audio> elements will happily play over each other the moment a switch races
// a load. So there is exactly one player: switching sources converts the
// playhead into scene time, converts it back into the new source's own time,
// and resumes only if it was already running.
//
// The sources are different things and are labelled as such: the original
// scene, the dubbed scene, a previously saved version of the whole dub, the
// processed line, the dry take before processing, and the sample a cloned
// voice was built from. Nothing here changes a render — the level-match
// option is an audition aid and says so.

export const SOURCES = [
  { kind: 'source', label: 'Original scene', window: 'source' },
  { kind: 'dub', label: 'Dubbed scene', window: 'target' },
  // A saved version is the whole file, so its own timeline *is* scene time.
  { kind: 'version', label: 'Previous version', window: 'target', absolute: true },
  // The two halves of the separation, so the bed under the dub and the voices
  // taken out of it can be checked at the same position as the finished mix.
  { kind: 'vocals', label: 'Original voices', window: 'target' },
  { kind: 'bed', label: 'Background bed', window: 'target' },
  { kind: 'line', label: 'Processed line' },
  // The two halves of an acoustic treatment. `dry` is the finished line before
  // the space was put around it, `treated` the same line with it — so the
  // effect can be heard on its own rather than only inside the mix. When no
  // treatment ran, `dry` is the final line and the preview label says so.
  { kind: 'dry', label: 'Dry line' },
  { kind: 'treated', label: 'With the space' },
  { kind: 'take', label: 'Dry take' },
  { kind: 'event', label: 'Reaction' },
  { kind: 'reference', label: 'Voice reference' },
];

const entry = kind => SOURCES.find(s => s.kind === kind);

export function createScenePlayer(audio) {
  let jobId = null, index = null, scene = null, kind = 'dub', take = '', version = '';
  let event = '';
  let matched = false, level = 0, listeners = [];

  const key = () => {
    const stored = safeGet('doblarr_api_key', '');
    return stored ? `&api_key=${encodeURIComponent(stored)}` : '';
  };

  function url() {
    if (kind === 'version') {
      return `${apiUrl(`jobs/${jobId}/versions/${version}/file`)}?compare=1${key()}`;
    }
    const query = new URLSearchParams({ context: String(scene?.context ?? 2) });
    if (kind === 'take' && take) query.set('take', take);
    if (kind === 'event' && event) query.set('event', event);
    return `${apiUrl(`jobs/${jobId}/preview/${kind}/${index}`)}?${query}${key()}`;
  }

  // The window a source's own timeline sits inside, or null when it has none
  // (a single line and a voice reference are not views of the scene).
  function windowFor(which) {
    const source = entry(which);
    if (!scene || !source?.window) return null;
    return (source.window === 'source' ? scene.source : scene.target) || null;
  }

  // Scene time: where the playhead is on the timeline the window describes.
  // A source excerpt and a dubbed excerpt can cover different intervals once
  // a review edit has moved a line, so converting through scene time is what
  // keeps A/B pointing at the same moment instead of at the same offset.
  function sceneTime() {
    const span = windowFor(kind);
    if (!span || !Number.isFinite(audio.currentTime)) return null;
    return entry(kind)?.absolute ? audio.currentTime : span.start + audio.currentTime;
  }

  function positionIn(which, moment) {
    const span = windowFor(which);
    if (span === null || moment === null) return 0;
    const local = entry(which)?.absolute ? moment : moment - span.start;
    return Math.max(0, Math.min(local, (span.end - span.start) - 0.05));
  }

  function emit(event, detail) {
    listeners.forEach(fn => fn(event, detail));
  }

  async function play(nextKind, { keepPosition = true, takeId = '', versionId = '',
    eventId = '' } = {}) {
    const wasPlaying = !audio.paused && !audio.ended;
    const moment = keepPosition ? sceneTime() : null;
    kind = nextKind;
    if (takeId) take = takeId;
    if (versionId) version = versionId;
    if (eventId) event = eventId;
    const position = positionIn(nextKind, moment);
    audio.pause();
    emit('loading', { kind });
    audio.src = url();
    applyLevel();
    try {
      await new Promise((resolve, reject) => {
        const ok = () => { cleanup(); resolve(); };
        const bad = () => { cleanup(); reject(new Error('This preview could not be played.')); };
        function cleanup() {
          audio.removeEventListener('loadedmetadata', ok);
          audio.removeEventListener('error', bad);
        }
        audio.addEventListener('loadedmetadata', ok, { once: true });
        audio.addEventListener('error', bad, { once: true });
        audio.load();
      });
    } catch (error) {
      emit('error', { kind, message: error.message });
      return false;
    }
    if (position && Number.isFinite(audio.duration)) {
      audio.currentTime = Math.min(position, Math.max(0, audio.duration - 0.05));
    }
    emit('ready', { kind, take, version });
    if (wasPlaying) {
      try { await audio.play(); } catch { /* the browser may require a gesture */ }
    }
    return true;
  }

  // Level matching is an audition aid for judging timbre, never a render.
  // Applying the inverse of the gain the level pass recorded makes two takes
  // comparable without pretending the production levels are the same.
  function applyLevel() {
    const undo = matched && level ? Math.pow(10, -level / 20) : 1;
    audio.volume = Math.max(0, Math.min(1, undo));
  }

  return {
    get kind() { return kind; },
    get take() { return take; },
    get event() { return event; },
    get version() { return version; },
    get matched() { return matched; },
    attach(nextJobId, nextIndex, nextScene, appliedDb = 0) {
      jobId = nextJobId; index = nextIndex; scene = nextScene;
      level = Number(appliedDb) || 0;
      take = scene?.selection?.take_id || '';
      event = scene?.events?.find(e => e.playable)?.event_id || '';
    },
    play,
    setVersion(id) { version = id || ''; },
    setLoop(on) { audio.loop = !!on; },
    setMatched(on) { matched = !!on; applyLevel(); },
    stop() {
      audio.pause();
      audio.removeAttribute('src');
      audio.load();
    },
    on(fn) { listeners.push(fn); },
    reset() {
      listeners = []; kind = 'dub'; take = ''; version = ''; event = '';
      matched = false;
    },
  };
}
