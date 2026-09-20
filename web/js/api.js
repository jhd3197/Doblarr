import { safeGet, safeSet } from './dom.js';

export function apiUrl(path) {
  return '/api/' + path.replace(/^\/?api\//, '').replace(/^\//, '');
}

function errorMessage(data, status) {
  if (typeof data?.error === 'string') return data.error;
  if (typeof data?.detail === 'string') return data.detail;
  if (Array.isArray(data?.detail))
    return data.detail.map(e => `${e.loc.join('.')}: ${e.msg}`).join('; ');
  return `Request failed (HTTP ${status})`;
}

// The only JSON transport. Native fetch remains untouched.
export async function api(path, options = {}) {
  const headers = new Headers(options.headers);
  const sentKey = safeGet('doblarr_api_key', '');
  if (sentKey) headers.set('X-Api-Key', sentKey);
  const { json, ...request } = options;
  if (json !== undefined) {
    headers.set('Content-Type', 'application/json');
    request.body = JSON.stringify(json);
  }
  const url = apiUrl(path);
  let response = await fetch(url, { ...request, headers });
  if (response.status === 401) {
    // Another concurrent request may already have refreshed the key.
    let key = safeGet('doblarr_api_key', '');
    if (key === sentKey) {
      key = window.prompt('Doblarr needs its API key (web.api_key in config.yaml):', '') || '';
      if (key) {
        safeSet('doblarr_api_key', key);
        window.dispatchEvent(new Event('doblarr-api-key'));
      }
    }
    if (key) {
      headers.set('X-Api-Key', key);
      response = await fetch(url, { ...request, headers });
    }
  }
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorMessage(data, response.status));
  if (data === null && response.status !== 204) throw new Error('The server returned invalid JSON.');
  return data;
}
