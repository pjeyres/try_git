// JSON-file backed store. All mutations go through here so they can be
// validated, persisted atomically and broadcast to connected clients.
import { randomUUID } from 'node:crypto';
import { readFileSync, writeFileSync, renameSync, mkdirSync, existsSync } from 'node:fs';
import { dirname } from 'node:path';

export const DIMENSIONS = ['impact', 'effort', 'cost'];
const MAX_TEXT = 2000;
const MAX_NAME = 60;

export class HttpError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function emptyState() {
  return { version: 0, members: [], lists: [], items: [] };
}

function cleanText(value, field, max = MAX_TEXT, { required = false } = {}) {
  if (value === undefined || value === null) {
    if (required) throw new HttpError(400, `${field} is required`);
    return '';
  }
  if (typeof value !== 'string') throw new HttpError(400, `${field} must be a string`);
  const text = value.trim();
  if (required && !text) throw new HttpError(400, `${field} is required`);
  if (text.length > max) throw new HttpError(400, `${field} is too long (max ${max})`);
  return text;
}

function cleanAmount(value) {
  if (value === undefined || value === null || value === '') return null;
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) throw new HttpError(400, 'costAmount must be a positive number');
  return Math.round(n * 100) / 100;
}

function cleanScore(value, field) {
  if (value === undefined || value === null) return null;
  const n = Number(value);
  if (!Number.isInteger(n) || n < 1 || n > 5) throw new HttpError(400, `${field} must be an integer from 1 to 5`);
  return n;
}

export class Store {
  constructor(file) {
    this.file = file;
    this.listeners = new Set();
    this.state = this.load();
  }

  load() {
    if (!this.file || !existsSync(this.file)) return emptyState();
    const data = JSON.parse(readFileSync(this.file, 'utf8'));
    return { ...emptyState(), ...data };
  }

  save() {
    if (!this.file) return;
    mkdirSync(dirname(this.file), { recursive: true });
    const tmp = `${this.file}.tmp`;
    writeFileSync(tmp, JSON.stringify(this.state, null, 2));
    renameSync(tmp, this.file);
  }

  commit() {
    this.state.version += 1;
    this.save();
    for (const fn of this.listeners) fn(this.state.version);
  }

  subscribe(fn) {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  }

  snapshot() {
    return this.state;
  }

  // ---- members -------------------------------------------------------------

  addMember(name) {
    const clean = cleanText(name, 'name', MAX_NAME, { required: true });
    const existing = this.state.members.find((m) => m.toLowerCase() === clean.toLowerCase());
    if (existing) return existing;
    this.state.members.push(clean);
    this.commit();
    return clean;
  }

  requireMember(name) {
    const member = this.state.members.find((m) => m === name);
    if (!member) throw new HttpError(400, `Unknown member "${name}"`);
    return member;
  }

  // ---- lists ---------------------------------------------------------------

  getList(id) {
    const list = this.state.lists.find((l) => l.id === id);
    if (!list) throw new HttpError(404, 'List not found');
    return list;
  }

  addList({ name }) {
    const list = {
      id: randomUUID(),
      name: cleanText(name, 'name', MAX_NAME, { required: true }),
      createdAt: new Date().toISOString(),
    };
    this.state.lists.push(list);
    this.commit();
    return list;
  }

  updateList(id, { name }) {
    const list = this.getList(id);
    if (name !== undefined) list.name = cleanText(name, 'name', MAX_NAME, { required: true });
    this.commit();
    return list;
  }

  deleteList(id) {
    this.getList(id);
    this.state.lists = this.state.lists.filter((l) => l.id !== id);
    this.state.items = this.state.items.filter((i) => i.listId !== id);
    this.commit();
  }

  // ---- items ---------------------------------------------------------------

  getItem(id) {
    const item = this.state.items.find((i) => i.id === id);
    if (!item) throw new HttpError(404, 'Item not found');
    return item;
  }

  addItem({ listId, title, notes, costAmount, createdBy }) {
    this.getList(listId);
    const now = new Date().toISOString();
    const item = {
      id: randomUUID(),
      listId,
      title: cleanText(title, 'title', 200, { required: true }),
      notes: cleanText(notes, 'notes'),
      costAmount: cleanAmount(costAmount),
      done: false,
      createdBy: createdBy ? this.requireMember(createdBy) : null,
      createdAt: now,
      updatedAt: now,
      scores: {},
    };
    this.state.items.push(item);
    this.commit();
    return item;
  }

  updateItem(id, patch) {
    const item = this.getItem(id);
    if (patch.title !== undefined) item.title = cleanText(patch.title, 'title', 200, { required: true });
    if (patch.notes !== undefined) item.notes = cleanText(patch.notes, 'notes');
    if (patch.costAmount !== undefined) item.costAmount = cleanAmount(patch.costAmount);
    if (patch.listId !== undefined) item.listId = this.getList(patch.listId).id;
    if (patch.done !== undefined) {
      if (typeof patch.done !== 'boolean') throw new HttpError(400, 'done must be a boolean');
      item.done = patch.done;
      item.doneAt = patch.done ? new Date().toISOString() : null;
    }
    item.updatedAt = new Date().toISOString();
    this.commit();
    return item;
  }

  deleteItem(id) {
    this.getItem(id);
    this.state.items = this.state.items.filter((i) => i.id !== id);
    this.commit();
  }

  // Each member scores an item independently; a null clears that dimension.
  setScores(id, member, scores) {
    const item = this.getItem(id);
    const who = this.requireMember(member);
    const current = { ...(item.scores[who] || {}) };
    for (const dim of DIMENSIONS) {
      if (dim in scores) {
        const value = cleanScore(scores[dim], dim);
        if (value === null) delete current[dim];
        else current[dim] = value;
      }
    }
    if (Object.keys(current).length) item.scores[who] = current;
    else delete item.scores[who];
    item.updatedAt = new Date().toISOString();
    this.commit();
    return item;
  }
}
