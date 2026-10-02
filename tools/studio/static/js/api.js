// JSON API wrapper. Errors carry the server's message, written for the user.
import { confirmDialog } from './ui.js';

export class ApiError extends Error {
  constructor(message, status, data) {
    super(message);
    this.status = status;
    this.data = data;
  }
}

async function request(method, url, body) {
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(url, options);
  } catch {
    throw new ApiError('The studio is not answering. Is studio.ps1 still running? Start it again and reload this page.', 0);
  }
  let data = null;
  try { data = await response.json(); } catch { /* not JSON */ }
  if (!response.ok) {
    throw new ApiError((data && data.error) || `The request failed (HTTP ${response.status}).`, response.status, data);
  }
  return data;
}

export const get = (url) => request('GET', url);
export const post = (url, body = {}) => request('POST', url, body);

/**
 * POST something that touches the Thor. When another app is in front the server answers 409;
 * the user is asked, and the request is repeated with confirm: true if they agree.
 * Resolves null when the user declined.
 */
export async function guardedPost(url, body = {}) {
  try {
    return await post(url, body);
  } catch (error) {
    if (error.status === 409 && error.data && error.data.needs_confirm) {
      const ok = await confirmDialog({
        title: 'The Thor may be in use',
        message: error.message,
        detail: error.data.foreground && error.data.foreground.activity,
        confirm: 'Run anyway',
        danger: true,
      });
      if (!ok) return null;
      return post(url, { ...body, confirm: true });
    }
    throw error;
  }
}
