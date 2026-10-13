// Minimal dependency-free HTTP server: JSON API + live updates (SSE) + static files.
import { createServer } from 'node:http';
import { createHmac, timingSafeEqual } from 'node:crypto';
import { readFile } from 'node:fs/promises';
import { extname, join, normalize, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Store, HttpError } from './store.js';

const ROOT = dirname(fileURLToPath(import.meta.url));
const PUBLIC_DIR = join(ROOT, 'public');
const MAX_BODY = 100 * 1024;
const COOKIE = 'im_auth';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.webmanifest': 'application/manifest+json',
  '.svg': 'image/svg+xml',
};

function send(res, status, body) {
  res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(body === undefined ? '' : JSON.stringify(body));
}

async function readJson(req) {
  let size = 0;
  const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY) throw new HttpError(413, 'Request body too large');
    chunks.push(chunk);
  }
  if (!chunks.length) return {};
  try {
    const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (data === null || typeof data !== 'object' || Array.isArray(data)) throw new Error();
    return data;
  } catch {
    throw new HttpError(400, 'Invalid JSON body');
  }
}

function parseCookies(header = '') {
  const out = {};
  for (const part of header.split(';')) {
    const i = part.indexOf('=');
    if (i > 0) out[part.slice(0, i).trim()] = decodeURIComponent(part.slice(i + 1).trim());
  }
  return out;
}

function safeEqual(a, b) {
  const ba = Buffer.from(String(a));
  const bb = Buffer.from(String(b));
  return ba.length === bb.length && timingSafeEqual(ba, bb);
}

export function createApp({ dataFile, passcode = '', currency = '€', secureCookie = false } = {}) {
  const store = new Store(dataFile);
  const authToken = passcode ? createHmac('sha256', passcode).update('impact-matrix-auth').digest('hex') : '';

  const isAuthed = (req) => !passcode || safeEqual(parseCookies(req.headers.cookie)[COOKIE] || '', authToken);

  // Route table: [method, pattern, handler(params, body, req)]
  const routes = [
    ['POST', /^\/api\/members$/, (_, b) => ({ name: store.addMember(b.name) })],
    ['POST', /^\/api\/lists$/, (_, b) => store.addList(b)],
    ['PATCH', /^\/api\/lists\/([\w-]+)$/, ([id], b) => store.updateList(id, b)],
    ['DELETE', /^\/api\/lists\/([\w-]+)$/, ([id]) => store.deleteList(id)],
    ['POST', /^\/api\/items$/, (_, b) => store.addItem(b)],
    ['PATCH', /^\/api\/items\/([\w-]+)$/, ([id], b) => store.updateItem(id, b)],
    ['DELETE', /^\/api\/items\/([\w-]+)$/, ([id]) => store.deleteItem(id)],
    ['PUT', /^\/api\/items\/([\w-]+)\/scores\/([^/]+)$/, ([id, member], b) => store.setScores(id, decodeURIComponent(member), b)],
  ];

  async function handleApi(req, res, path) {
    if (req.method === 'GET' && path === '/api/config') {
      return send(res, 200, { authRequired: Boolean(passcode), authed: isAuthed(req), currency });
    }

    if (req.method === 'POST' && path === '/api/login') {
      const body = await readJson(req);
      if (!passcode || safeEqual(body.passcode || '', passcode)) {
        const flags = `Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000${secureCookie ? '; Secure' : ''}`;
        res.setHeader('Set-Cookie', `${COOKIE}=${authToken}; ${flags}`);
        return send(res, 200, { ok: true });
      }
      throw new HttpError(401, 'Wrong passcode');
    }

    if (!isAuthed(req)) throw new HttpError(401, 'Not logged in');

    if (req.method === 'GET' && path === '/api/state') return send(res, 200, store.snapshot());

    if (req.method === 'GET' && path === '/api/events') {
      res.writeHead(200, {
        'Content-Type': 'text/event-stream',
        'Cache-Control': 'no-store',
        Connection: 'keep-alive',
        'X-Accel-Buffering': 'no',
      });
      res.write(`event: version\ndata: ${store.snapshot().version}\n\n`);
      const unsubscribe = store.subscribe((v) => res.write(`event: version\ndata: ${v}\n\n`));
      const ping = setInterval(() => res.write(': ping\n\n'), 25000);
      req.on('close', () => {
        clearInterval(ping);
        unsubscribe();
      });
      return;
    }

    for (const [method, pattern, handler] of routes) {
      const match = path.match(pattern);
      if (match && req.method === method) {
        const body = ['POST', 'PATCH', 'PUT'].includes(method) ? await readJson(req) : {};
        const result = handler(match.slice(1), body, req);
        return send(res, result === undefined ? 204 : 200, result);
      }
    }
    throw new HttpError(404, 'Not found');
  }

  async function serveStatic(req, res, path) {
    if (req.method !== 'GET' && req.method !== 'HEAD') throw new HttpError(405, 'Method not allowed');
    const rel = normalize(path === '/' ? '/index.html' : path).replace(/^(\.\.[/\\])+/, '');
    const file = join(PUBLIC_DIR, rel);
    if (!file.startsWith(PUBLIC_DIR)) throw new HttpError(404, 'Not found');
    try {
      const data = await readFile(file);
      res.writeHead(200, { 'Content-Type': MIME[extname(file)] || 'application/octet-stream', 'Cache-Control': 'no-cache' });
      res.end(req.method === 'HEAD' ? undefined : data);
    } catch {
      throw new HttpError(404, 'Not found');
    }
  }

  const server = createServer(async (req, res) => {
    const path = new URL(req.url, 'http://localhost').pathname;
    try {
      if (path.startsWith('/api/')) await handleApi(req, res, path);
      else await serveStatic(req, res, path);
    } catch (err) {
      const status = err instanceof HttpError ? err.status : 500;
      if (status === 500) console.error(err);
      if (!res.headersSent) send(res, status, { error: status === 500 ? 'Internal server error' : err.message });
      else res.end();
    }
  });

  return { server, store };
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  const port = Number(process.env.PORT) || 3000;
  const { server } = createApp({
    dataFile: process.env.DATA_FILE || join(ROOT, 'data', 'db.json'),
    passcode: process.env.APP_PASSCODE || '',
    currency: process.env.CURRENCY || '€',
    secureCookie: process.env.SECURE_COOKIE === '1',
  });
  server.listen(port, () => {
    console.log(`Impact matrix running on http://localhost:${port}`);
    if (!process.env.APP_PASSCODE) console.log('Tip: set APP_PASSCODE to require a shared passcode.');
  });
}
