import { safeGet, safeSet } from './dom.js';

// "Star us on GitHub": a quiet star next to the logo, plus a one-time
// sidebar nudge. Starring (or a click through) hides the nudge for good;
// "Not now" snoozes it so it doesn't nag on every visit.
const REPO_URL = "https://github.com/jhd3197/Doblarr";
const STARRED_KEY = "doblarr.starNudge.starred";
const SNOOZE_KEY = "doblarr.starNudge.snoozedUntil";
const SNOOZE_MS = 14 * 24 * 60 * 60 * 1000;

function nudgeDue(now = Date.now()) {
  if (safeGet(STARRED_KEY, "") === "1") return false;
  const until = Number(safeGet(SNOOZE_KEY, "0")) || 0;
  return now >= until;
}

function initStar() {
  const nudge = document.getElementById("starNudge");
  const hide = () => { if (nudge) nudge.hidden = true; };
  const markStarred = () => { safeSet(STARRED_KEY, "1"); hide(); };

  document.getElementById("brandStar")?.addEventListener("click", markStarred);
  if (!nudge) return;
  nudge.querySelector("[data-star-go]")?.addEventListener("click", markStarred);
  nudge.querySelector("[data-star-later]")?.addEventListener("click", () => {
    safeSet(SNOOZE_KEY, String(Date.now() + SNOOZE_MS));
    hide();
  });
  nudge.hidden = !nudgeDue();
}

export { initStar, nudgeDue, REPO_URL };
