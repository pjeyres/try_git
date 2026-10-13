import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createApp } from '../server.js';

let dir;
let base;
let app;

async function call(method, path, body, headers = {}) {
  const res = await fetch(base + path, {
    method,
    headers: { ...(body ? { 'Content-Type': 'application/json' } : {}), ...headers },
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  return { status: res.status, body: text ? JSON.parse(text) : null, headers: res.headers };
}

function listen(options) {
  const instance = createApp(options);
  return new Promise((resolve) => {
    instance.server.listen(0, '127.0.0.1', () => resolve({ ...instance, url: `http://127.0.0.1:${instance.server.address().port}` }));
  });
}

before(async () => {
  dir = mkdtempSync(join(tmpdir(), 'impact-matrix-'));
  app = await listen({ dataFile: join(dir, 'db.json') });
  base = app.url;
});

after(() => {
  app.server.closeAllConnections?.();
  app.server.close();
  rmSync(dir, { recursive: true, force: true });
});

test('full flow: members, lists, items, scores, persistence', async () => {
  assert.equal((await call('POST', '/api/members', { name: 'Peter' })).body.name, 'Peter');
  assert.equal((await call('POST', '/api/members', { name: 'Anna' })).body.name, 'Anna');
  // Names are de-duplicated case-insensitively.
  assert.equal((await call('POST', '/api/members', { name: 'peter' })).body.name, 'Peter');

  const list = (await call('POST', '/api/lists', { name: 'House' })).body;
  const item = (await call('POST', '/api/items', { listId: list.id, title: 'Paint the hallway', costAmount: '120.5', createdBy: 'Peter' })).body;
  assert.equal(item.costAmount, 120.5);
  assert.deepEqual(item.scores, {});

  let r = await call('PUT', `/api/items/${item.id}/scores/Peter`, { impact: 4, effort: 3, cost: 2 });
  assert.equal(r.status, 200);
  r = await call('PUT', `/api/items/${item.id}/scores/Anna`, { impact: 2 });
  assert.deepEqual(r.body.scores, { Peter: { impact: 4, effort: 3, cost: 2 }, Anna: { impact: 2 } });

  // null clears a single dimension; clearing everything removes the member's entry.
  r = await call('PUT', `/api/items/${item.id}/scores/Anna`, { impact: null });
  assert.deepEqual(r.body.scores, { Peter: { impact: 4, effort: 3, cost: 2 } });

  r = await call('PATCH', `/api/items/${item.id}`, { done: true, notes: 'Use the grey paint' });
  assert.equal(r.body.done, true);
  assert.ok(r.body.doneAt);

  const state = (await call('GET', '/api/state')).body;
  assert.equal(state.items.length, 1);
  const saved = JSON.parse(readFileSync(join(dir, 'db.json'), 'utf8'));
  assert.equal(saved.version, state.version);
  assert.equal(saved.items[0].notes, 'Use the grey paint');

  assert.equal((await call('DELETE', `/api/lists/${list.id}`)).status, 204);
  assert.equal((await call('GET', '/api/state')).body.items.length, 0);
});

test('validation errors', async () => {
  const list = (await call('POST', '/api/lists', { name: 'Garden' })).body;
  const item = (await call('POST', '/api/items', { listId: list.id, title: 'Weed' })).body;

  assert.equal((await call('POST', '/api/items', { listId: list.id, title: '   ' })).status, 400);
  assert.equal((await call('POST', '/api/items', { listId: 'nope', title: 'x' })).status, 404);
  assert.equal((await call('PUT', `/api/items/${item.id}/scores/Peter`, { impact: 6 })).status, 400);
  assert.equal((await call('PUT', `/api/items/${item.id}/scores/Peter`, { effort: 2.5 })).status, 400);
  assert.equal((await call('PUT', `/api/items/${item.id}/scores/Stranger`, { impact: 3 })).status, 400);
  assert.equal((await call('PATCH', `/api/items/${item.id}`, { costAmount: -5 })).status, 400);
  assert.equal((await call('PATCH', `/api/items/${item.id}`, { done: 'yes' })).status, 400);
  const bad = await fetch(`${base}/api/lists`, { method: 'POST', body: '{nope', headers: { 'Content-Type': 'application/json' } });
  assert.equal(bad.status, 400);
});

test('serves the app and blocks path traversal', async () => {
  const res = await fetch(`${base}/`);
  assert.equal(res.status, 200);
  assert.match(await res.text(), /Impact Matrix/);
  assert.equal((await fetch(`${base}/scoring.js`)).headers.get('content-type'), 'text/javascript; charset=utf-8');
  assert.equal((await fetch(`${base}/..%2Fserver.js`)).status, 404);
});

test('live updates are pushed over server-sent events', async () => {
  const controller = new AbortController();
  const res = await fetch(`${base}/api/events`, { signal: controller.signal });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  const nextVersion = async () => {
    while (!/data: (\d+)\n\n/.test(buffer)) buffer += decoder.decode((await reader.read()).value);
    const [, v] = buffer.match(/data: (\d+)\n\n/);
    buffer = buffer.slice(buffer.indexOf('\n\n') + 2);
    return Number(v);
  };
  const initial = await nextVersion();
  await call('POST', '/api/lists', { name: 'Holiday' });
  assert.equal(await nextVersion(), initial + 1);
  controller.abort();
});

test('passcode protects the API', async () => {
  const locked = await listen({ passcode: 'open sesame' });
  const url = locked.url;
  try {
    const config = await (await fetch(`${url}/api/config`)).json();
    assert.deepEqual(config, { authRequired: true, authed: false, currency: '€' });
    assert.equal((await fetch(`${url}/api/state`)).status, 401);

    const wrong = await fetch(`${url}/api/login`, { method: 'POST', body: JSON.stringify({ passcode: 'nope' }) });
    assert.equal(wrong.status, 401);

    const ok = await fetch(`${url}/api/login`, { method: 'POST', body: JSON.stringify({ passcode: 'open sesame' }) });
    assert.equal(ok.status, 200);
    const cookie = ok.headers.get('set-cookie').split(';')[0];
    assert.equal((await fetch(`${url}/api/state`, { headers: { cookie } })).status, 200);
    assert.equal((await fetch(`${url}/api/state`, { headers: { cookie: 'im_auth=forged' } })).status, 401);
  } finally {
    locked.server.close();
  }
});
