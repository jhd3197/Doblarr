export const state = { page: "Overview", tab: "connections", keys: false, vals: {}, config: null, search: "" };
export const library = { items: [], filter: "all", sort: "default", activity: {}, loaded: false };
export const jobs = { pollTimer: null };
export const titleState = { item: null, dtab: "plan", plan: null, planStatus: "" };
export const castState = { item: null, cast: [], voices: null };
function matchesSearch(title) {
  return !state.search || String(title).toLowerCase().includes(state.search);
}

function cfgGet(dotted) {
  if (!state.config) return undefined;
  return dotted.split(".").reduce((o, k) => (o && o[k] !== undefined) ? o[k] : undefined, state.config);
}

export { matchesSearch, cfgGet };
