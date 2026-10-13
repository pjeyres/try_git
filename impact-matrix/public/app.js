import {
  DIMENSIONS, LABELS, QUADRANTS, MIDPOINT,
  scoresFor, disagreement, missingScorers, xValue, priority, quadrant, rankItems,
} from './scoring.js';

const $ = (sel) => document.querySelector(sel);
const SVG_NS = 'http://www.w3.org/2000/svg';

// ---- local preferences (per device) ------------------------------------------

const prefs = {
  get(key, fallback) {
    try {
      const v = localStorage.getItem(`im.${key}`);
      return v === null ? fallback : JSON.parse(v);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try { localStorage.setItem(`im.${key}`, JSON.stringify(value)); } catch { /* private mode */ }
  },
};

const ui = {
  me: prefs.get('me', null),
  listId: prefs.get('listId', null),
  view: prefs.get('view', 'matrix'),
  scoreView: prefs.get('scoreView', 'combined'),
  xAxis: prefs.get('xAxis', 'effort'),
  costWeight: prefs.get('costWeight', 0.5),
  showDone: prefs.get('showDone', false),
  openItemId: null,
};

let state = { version: -1, members: [], lists: [], items: [] };
let config = { currency: '€' };

// ---- API -----------------------------------------------------------------------

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && path !== '/api/login') {
    showScreen('login');
    throw new Error('Please log in');
  }
  const data = res.status === 204 ? null : await res.json();
  if (!res.ok) throw new Error(data?.error || `Request failed (${res.status})`);
  return data;
}

async function mutate(method, path, body) {
  try {
    const result = await api(method, path, body);
    await refresh();
    return result;
  } catch (err) {
    toast(err.message);
    throw err;
  }
}

async function refresh() {
  const next = await api('GET', '/api/state');
  if (next.version === state.version) return;
  state = next;
  render();
}

function connectLive() {
  const source = new EventSource('/api/events');
  source.addEventListener('version', (e) => {
    if (Number(e.data) !== state.version) refresh().catch(() => {});
  });
  // EventSource reconnects on its own; resync whenever it does.
  source.addEventListener('open', () => refresh().catch(() => {}));
}

// ---- helpers -------------------------------------------------------------------

function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (k === 'class') el.className = v;
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const c of children.flat()) if (c != null && c !== false) el.append(c);
  return el;
}

function svg(tag, attrs = {}, ...children) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else if (v != null) el.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) el.append(c);
  return el;
}

function fmt(n) {
  return n == null ? '–' : Number.isInteger(n) ? String(n) : n.toFixed(1);
}

function money(amount) {
  if (amount == null) return '';
  return `${config.currency}${amount.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function costColor(cost) {
  if (cost == null) return 'var(--unscored)';
  // green (cheap) → amber → red (expensive)
  const hue = 140 - ((cost - 1) / 4) * 135;
  return `hsl(${hue} 70% 45%)`;
}

let toastTimer;
function toast(message) {
  const el = $('#toast');
  el.textContent = message;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3500);
}

function showScreen(name) {
  for (const id of ['login', 'whoami', 'app']) $(`#${id}`).hidden = id !== name;
}

function currentList() {
  return state.lists.find((l) => l.id === ui.listId) || state.lists[0] || null;
}

function visibleItems() {
  const list = currentList();
  if (!list) return [];
  return state.items.filter((i) => i.listId === list.id && (ui.showDone || !i.done));
}

function partnerNames() {
  return state.members.filter((m) => m !== ui.me);
}

// ---- rendering -----------------------------------------------------------------

function render() {
  if (!ui.me || !state.members.includes(ui.me)) {
    renderWhoAmI();
    showScreen('whoami');
    return;
  }
  showScreen('app');
  $('#me-chip').textContent = ui.me;

  const list = currentList();
  if (list && list.id !== ui.listId) {
    ui.listId = list.id;
    prefs.set('listId', ui.listId);
  }
  renderTabs();
  $('#empty-lists').hidden = Boolean(list);
  $('#list-view').hidden = !list;
  if (!list) return;

  renderControls();
  renderBanner();
  $('#matrix-panel').hidden = ui.view !== 'matrix';
  $('#ranked-panel').hidden = ui.view !== 'ranked';
  if (ui.view === 'matrix') renderMatrix();
  else renderRanked();
  renderDialog();
}

function renderWhoAmI() {
  const box = $('#member-buttons');
  box.replaceChildren(...state.members.map((name) =>
    h('button', { class: 'big', onclick: () => chooseMe(name) }, `I’m ${name}`)));
}

function chooseMe(name) {
  ui.me = name;
  prefs.set('me', name);
  if (ui.scoreView !== 'combined' && !state.members.includes(ui.scoreView)) ui.scoreView = 'combined';
  render();
}

function renderTabs() {
  const tabs = state.lists.map((list) => {
    const active = list.id === ui.listId;
    const open = state.items.filter((i) => i.listId === list.id && !i.done).length;
    return h('div', { class: `tab${active ? ' active' : ''}` },
      h('button', { class: 'tab-name', onclick: () => selectList(list.id) }, list.name, h('span', { class: 'count' }, String(open))),
      active && h('button', { class: 'tab-menu', title: 'Rename or delete list', 'aria-label': 'List options', onclick: () => listMenu(list) }, '⋯'));
  });
  tabs.push(h('button', { class: 'tab add', title: 'New list', 'aria-label': 'New list', onclick: newList }, '+'));
  $('#list-tabs').replaceChildren(...tabs);
}

function renderControls() {
  for (const btn of $('#view-toggle').children) {
    btn.classList.toggle('on', btn.dataset.value === ui.view);
    btn.setAttribute('aria-selected', btn.dataset.value === ui.view);
  }
  const select = $('#score-view');
  const options = [['combined', 'Combined'], ...state.members.map((m) => [m, m === ui.me ? `Mine (${m})` : m])];
  select.replaceChildren(...options.map(([value, label]) => h('option', { value }, label)));
  select.value = options.some(([v]) => v === ui.scoreView) ? ui.scoreView : 'combined';
  $('#x-axis').value = ui.xAxis;
  $('#cost-weight').value = Math.round(ui.costWeight * 100);
  $('#cost-weight-out').textContent = `${Math.round(ui.costWeight * 100)}%`;
  $('#show-done').checked = ui.showDone;
}

function renderBanner() {
  const items = visibleItems().filter((i) => !i.done);
  const mine = items.filter((i) => missingScorers(i, [ui.me]).length);
  const debates = items.filter((i) => disagreement(i) >= 2);
  const parts = [];
  if (mine.length) parts.push(h('button', { class: 'link', onclick: () => openItem(mine[0].id) }, `${mine.length} task${mine.length > 1 ? 's need' : ' needs'} your scores`));
  if (debates.length) parts.push(h('button', { class: 'link', onclick: () => openItem(debates[0].id) }, `${debates.length} to talk about (you scored very differently)`));
  const banner = $('#todo-banner');
  banner.hidden = !parts.length;
  banner.replaceChildren(...parts.flatMap((p, i) => (i ? [' · ', p] : [p])));
}

function renderMatrix() {
  const container = $('#matrix');
  const W = Math.max(container.clientWidth || 320, 300);
  const H = Math.round(Math.min(Math.max(W * (W < 600 ? 1.15 : 0.75), 340), 640));
  const m = { top: 16, right: 16, bottom: 44, left: 44 };
  const pw = W - m.left - m.right;
  const ph = H - m.top - m.bottom;
  const sx = (v) => m.left + ((v - 0.5) / 5) * pw;
  const sy = (v) => m.top + (1 - (v - 0.5) / 5) * ph;

  const axisName = { effort: 'Effort', cost: 'Cost', blend: 'Effort + cost' }[ui.xAxis];
  const root = svg('svg', { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: 'img', 'aria-label': `Impact versus ${axisName} matrix` });

  // Quadrant backgrounds and labels.
  const midX = sx(MIDPOINT), midY = sy(MIDPOINT);
  const quads = [
    ['quickWin', m.left, m.top, midX - m.left, midY - m.top, 'start'],
    ['bigProject', midX, m.top, W - m.right - midX, midY - m.top, 'end'],
    ['fillIn', m.left, midY, midX - m.left, H - m.bottom - midY, 'start'],
    ['timeSink', midX, midY, W - m.right - midX, H - m.bottom - midY, 'end'],
  ];
  for (const [key, x, y, w, hh, anchor] of quads) {
    root.append(svg('rect', { x, y, width: w, height: hh, class: `quad q-${key}` }));
    const tx = anchor === 'start' ? x + 8 : x + w - 8;
    root.append(svg('text', { x: tx, y: y + 18, 'text-anchor': anchor, class: 'quad-label' }, QUADRANTS[key].name));
    root.append(svg('text', { x: tx, y: y + 32, 'text-anchor': anchor, class: 'quad-hint' }, QUADRANTS[key].hint));
  }

  // Grid lines and ticks.
  for (let v = 1; v <= 5; v++) {
    root.append(svg('line', { x1: sx(v), x2: sx(v), y1: m.top, y2: H - m.bottom, class: 'grid' }));
    root.append(svg('line', { x1: m.left, x2: W - m.right, y1: sy(v), y2: sy(v), class: 'grid' }));
    root.append(svg('text', { x: sx(v), y: H - m.bottom + 16, 'text-anchor': 'middle', class: 'tick' }, String(v)));
    root.append(svg('text', { x: m.left - 10, y: sy(v) + 4, 'text-anchor': 'end', class: 'tick' }, String(v)));
  }
  root.append(svg('text', { x: m.left + pw / 2, y: H - 6, 'text-anchor': 'middle', class: 'axis-label' }, `${axisName} →`));
  root.append(svg('text', { x: 14, y: m.top + ph / 2, 'text-anchor': 'middle', class: 'axis-label', transform: `rotate(-90 14 ${m.top + ph / 2})` }, 'Impact →'));

  // Plot items; spread out items that land on the same spot.
  const placed = [];
  const unscored = [];
  for (const item of visibleItems()) {
    const s = scoresFor(item, ui.scoreView);
    const x = xValue(s, ui.xAxis, ui.costWeight);
    if (s.impact == null || x == null) unscored.push(item);
    else placed.push({ item, s, x, y: s.impact });
  }
  const buckets = new Map();
  for (const p of placed) {
    const key = `${p.x.toFixed(2)}|${p.y.toFixed(2)}`;
    if (!buckets.has(key)) buckets.set(key, []);
    buckets.get(key).push(p);
  }
  // Size encodes cost (or effort when cost is the x axis).
  const sizeDim = ui.xAxis === 'cost' ? 'effort' : 'cost';
  const dots = [];
  for (const group of buckets.values()) {
    group.forEach((p, i) => {
      const spread = group.length > 1 ? 16 + group.length * 2 : 0;
      const angle = (i / group.length) * Math.PI * 2 - Math.PI / 2;
      const size = p.s[sizeDim];
      dots.push({
        ...p,
        cx: sx(p.x) + Math.cos(angle) * spread,
        cy: sy(p.y) + Math.sin(angle) * spread,
        r: size == null ? 7 : 6 + size * 2.5,
      });
    });
  }

  // Place each label beside its bubble where it collides least with bubbles,
  // other labels and the quadrant captions (biggest bubbles get first pick).
  const maxChars = W < 600 ? 16 : 22;
  for (const d of dots) d.box = { x1: d.cx - d.r, y1: d.cy - d.r, x2: d.cx + d.r, y2: d.cy + d.r };
  const obstacles = [
    ...quads.map(([, x, y, w, , anchor]) => (anchor === 'start'
      ? { x1: x, y1: y, x2: x + 150, y2: y + 36 }
      : { x1: x + w - 150, y1: y, x2: x + w, y2: y + 36 })),
    ...dots.map((d) => d.box),
  ];
  const overlap = (a, b) => Math.max(0, Math.min(a.x2, b.x2) - Math.max(a.x1, b.x1)) * Math.max(0, Math.min(a.y2, b.y2) - Math.max(a.y1, b.y1));
  for (const d of [...dots].sort((a, b) => b.r - a.r)) {
    d.label = d.item.title.length > maxChars ? `${d.item.title.slice(0, maxChars - 1)}…` : d.item.title;
    const tw = d.label.length * 6.6;
    const candidates = [
      { x: d.cx + d.r + 4, y: d.cy + 4, anchor: 'start' },
      { x: d.cx - d.r - 4, y: d.cy + 4, anchor: 'end' },
      { x: d.cx, y: d.cy - d.r - 6, anchor: 'middle' },
      { x: d.cx, y: d.cy + d.r + 14, anchor: 'middle' },
    ].map((c) => {
      const x1 = c.anchor === 'start' ? c.x : c.anchor === 'end' ? c.x - tw : c.x - tw / 2;
      const box = { x1, x2: x1 + tw, y1: c.y - 11, y2: c.y + 3 };
      const outside = Math.max(0, m.left - box.x1) + Math.max(0, box.x2 - (W - m.right));
      const cost = obstacles.reduce((sum, o) => sum + (o === d.box ? 0 : overlap(box, o)), 0) + outside * 50;
      return { ...c, box, cost };
    });
    d.labelPos = candidates.reduce((a, b) => (b.cost < a.cost ? b : a));
    obstacles.push(d.labelPos.box);
  }

  for (const { item, s, cx, cy, r, label, labelPos } of dots) {
    const debate = disagreement(item) >= 2;
    root.append(svg('g', {
      class: `dot${item.done ? ' done' : ''}${debate ? ' debate' : ''}`,
      tabindex: 0,
      role: 'button',
      onclick: () => openItem(item.id),
      onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openItem(item.id); } },
    },
    svg('title', {}, `${item.title}\nImpact ${fmt(s.impact)} · Effort ${fmt(s.effort)} · Cost ${fmt(s.cost)}${item.costAmount != null ? ` (${money(item.costAmount)})` : ''}`),
    debate ? svg('circle', { cx, cy, r: r + 4, class: 'debate-ring' }) : null,
    svg('circle', { cx, cy, r, fill: costColor(s.cost), class: s.cost == null ? 'no-cost' : '' }),
    svg('text', { x: labelPos.x, y: labelPos.y, 'text-anchor': labelPos.anchor, class: 'dot-label' }, label)));
  }
  container.replaceChildren(root);

  $('#legend').replaceChildren(
    h('span', {}, `Bubble size: ${sizeDim}`),
    h('span', { class: 'swatches' }, 'Colour: cost ', ...[1, 2, 3, 4, 5].map((c) => h('i', { style: `background:${costColor(c)}`, title: LABELS.cost[c - 1] })), h('i', { style: 'background:var(--unscored)', title: 'Not scored' })),
    h('span', {}, h('i', { class: 'ring-sample' }), ' you disagree'));

  const unscoredEl = $('#unscored');
  unscoredEl.replaceChildren(...(unscored.length
    ? [h('h3', {}, `Not scored yet${ui.scoreView === 'combined' ? '' : ` by ${ui.scoreView}`}`),
      h('div', { class: 'pills' }, unscored.map((item) => h('button', { class: 'pill', onclick: () => openItem(item.id) }, item.title)))]
    : []));
}

function renderRanked() {
  const ranked = rankItems(visibleItems(), ui.scoreView, ui.costWeight);
  const list = $('#ranked');
  if (!ranked.length) {
    list.replaceChildren(h('li', { class: 'empty muted' }, 'Nothing here yet — add a task above.'));
    return;
  }
  list.replaceChildren(...ranked.map(({ item, scores, priority: p }) => {
    const q = quadrant(scores, ui.xAxis, ui.costWeight);
    const missing = missingScorers(item, state.members);
    return h('li', { class: `row-item${item.done ? ' done' : ''}` },
      h('input', { type: 'checkbox', checked: item.done, 'aria-label': 'Done', onchange: (e) => setDone(item, e.target.checked) }),
      h('button', { class: 'row-main', onclick: () => openItem(item.id) },
        h('span', { class: 'row-title' }, item.title),
        h('span', { class: 'row-meta' },
          q && h('span', { class: `tag q-${q}` }, QUADRANTS[q].name),
          h('span', {}, `I ${fmt(scores.impact)} · E ${fmt(scores.effort)} · C ${fmt(scores.cost)}`),
          item.costAmount != null && h('span', {}, money(item.costAmount)),
          disagreement(item) >= 2 && h('span', { class: 'tag debate' }, 'talk about it'),
          missing.length > 0 && h('span', { class: 'tag waiting' }, `waiting on ${missing.join(' & ')}`))),
      h('span', { class: 'score', title: 'Priority: impact ÷ (effort/cost blend)' }, p == null ? '–' : `${p.toFixed(1)}×`));
  }));
}

// ---- item dialog -----------------------------------------------------------------

function openItem(id) {
  ui.openItemId = id;
  const item = state.items.find((i) => i.id === id);
  if (!item) return;
  const form = $('#item-form');
  form.title.value = item.title;
  form.notes.value = item.notes;
  form.costAmount.value = item.costAmount ?? '';
  form.listId.replaceChildren(...state.lists.map((l) => h('option', { value: l.id }, l.name)));
  form.listId.value = item.listId;
  $('#currency').textContent = config.currency;
  renderDialog();
  const dialog = $('#item-dialog');
  if (!dialog.open) dialog.showModal();
}

// Re-renders the live parts of the dialog (scores, done state) without
// clobbering text the user may be typing.
function renderDialog() {
  const dialog = $('#item-dialog');
  if (!dialog.open && !ui.openItemId) return;
  const item = state.items.find((i) => i.id === ui.openItemId);
  if (!item) {
    if (dialog.open) { dialog.close(); toast('That task was deleted'); }
    ui.openItemId = null;
    return;
  }
  const mine = item.scores[ui.me] || {};
  $('#my-scores').replaceChildren(
    h('h3', {}, 'Your scores'),
    ...DIMENSIONS.map((dim) => h('div', { class: 'score-row' },
      h('span', { class: 'dim' }, dim[0].toUpperCase() + dim.slice(1)),
      h('div', { class: `pips dim-${dim}`, role: 'radiogroup', 'aria-label': dim },
        [1, 2, 3, 4, 5].map((v) => h('button', {
          type: 'button',
          role: 'radio',
          'aria-checked': String(mine[dim] === v),
          class: mine[dim] === v ? 'on' : '',
          title: LABELS[dim][v - 1],
          onclick: () => setScore(item, dim, mine[dim] === v ? null : v),
        }, String(v)))),
      h('span', { class: 'pip-label muted' }, mine[dim] ? LABELS[dim][mine[dim] - 1] : 'not set'))));

  const others = partnerNames().map((name) => {
    const s = item.scores[name];
    return h('p', {}, h('strong', {}, name), ': ', s ? DIMENSIONS.map((d) => `${d} ${fmt(s[d] ?? null)}`).join(' · ') : 'hasn’t scored yet');
  });
  const combined = scoresFor(item, 'combined');
  const p = priority(combined, ui.costWeight);
  const q = quadrant(combined, ui.xAxis, ui.costWeight);
  others.push(h('p', { class: 'muted' }, `Combined: impact ${fmt(combined.impact)} · effort ${fmt(combined.effort)} · cost ${fmt(combined.cost)}`,
    p != null ? ` → ${p.toFixed(1)}× priority` : '', q ? ` · ${QUADRANTS[q].name}` : ''));
  if (disagreement(item) >= 2) others.push(h('p', { class: 'tag debate' }, 'You scored this quite differently — worth a chat.'));
  $('#other-scores').replaceChildren(...others);

  $('#toggle-done').textContent = item.done ? 'Mark not done' : 'Mark done ✓';
  $('#toggle-done').className = item.done ? '' : 'primary';
}

async function saveField(field, value) {
  const item = state.items.find((i) => i.id === ui.openItemId);
  if (!item) return;
  const current = field === 'costAmount' ? (item.costAmount ?? '') : item[field];
  if (String(current) === String(value)) return;
  await mutate('PATCH', `/api/items/${item.id}`, { [field]: value === '' && field === 'costAmount' ? null : value }).catch(() => {});
}

function setScore(item, dim, value) {
  return mutate('PUT', `/api/items/${item.id}/scores/${encodeURIComponent(ui.me)}`, { [dim]: value }).catch(() => {});
}

function setDone(item, done) {
  return mutate('PATCH', `/api/items/${item.id}`, { done }).catch(() => {});
}

// ---- list actions ------------------------------------------------------------------

function selectList(id) {
  ui.listId = id;
  prefs.set('listId', id);
  render();
}

async function newList() {
  const name = prompt('Name for the new list:');
  if (!name?.trim()) return;
  const list = await mutate('POST', '/api/lists', { name }).catch(() => null);
  if (list) selectList(list.id);
}

async function listMenu(list) {
  const name = prompt(`Rename “${list.name}” (or type DELETE to delete it and all its tasks):`, list.name);
  if (name == null || name.trim() === list.name) return;
  if (name.trim() === 'DELETE') {
    if (confirm(`Delete “${list.name}” and all its tasks? This can’t be undone.`)) {
      await mutate('DELETE', `/api/lists/${list.id}`).catch(() => {});
    }
    return;
  }
  await mutate('PATCH', `/api/lists/${list.id}`, { name }).catch(() => {});
}

// ---- wiring --------------------------------------------------------------------------

function bind() {
  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const err = $('#login-error');
    try {
      await api('POST', '/api/login', { passcode: e.target.passcode.value });
      err.hidden = true;
      await start();
    } catch (ex) {
      err.textContent = ex.message;
      err.hidden = false;
    }
  });

  $('#member-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const res = await mutate('POST', '/api/members', { name: e.target.name.value }).catch(() => null);
    if (res) { e.target.reset(); chooseMe(res.name); }
  });

  $('#me-chip').addEventListener('click', () => {
    ui.me = null;
    prefs.set('me', null);
    render();
  });

  $('#first-list-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const list = await mutate('POST', '/api/lists', { name: e.target.name.value }).catch(() => null);
    if (list) { e.target.reset(); selectList(list.id); }
  });

  $('#add-item-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const title = e.target.title.value;
    const item = await mutate('POST', '/api/items', { listId: ui.listId, title, createdBy: ui.me }).catch(() => null);
    if (item) { e.target.reset(); openItem(item.id); }
  });

  $('#view-toggle').addEventListener('click', (e) => {
    const value = e.target.closest('button')?.dataset.value;
    if (!value) return;
    ui.view = value;
    prefs.set('view', value);
    render();
  });

  const onPref = (sel, key, read) => $(sel).addEventListener('input', (e) => {
    ui[key] = read(e.target);
    prefs.set(key, ui[key]);
    render();
  });
  onPref('#score-view', 'scoreView', (t) => t.value);
  onPref('#x-axis', 'xAxis', (t) => t.value);
  onPref('#cost-weight', 'costWeight', (t) => Number(t.value) / 100);
  onPref('#show-done', 'showDone', (t) => t.checked);

  const form = $('#item-form');
  for (const field of ['title', 'notes', 'costAmount', 'listId']) {
    form[field].addEventListener('change', (e) => saveField(field, e.target.value));
  }
  $('#toggle-done').addEventListener('click', () => {
    const item = state.items.find((i) => i.id === ui.openItemId);
    if (item) setDone(item, !item.done);
  });
  $('#delete-item').addEventListener('click', async () => {
    const item = state.items.find((i) => i.id === ui.openItemId);
    if (!item || !confirm(`Delete “${item.title}”?`)) return;
    ui.openItemId = null;
    $('#item-dialog').close();
    await mutate('DELETE', `/api/items/${item.id}`).catch(() => {});
  });
  $('#item-dialog').addEventListener('close', () => {
    // Save whichever field still has focus before forgetting the item.
    const active = document.activeElement;
    if (active?.form === form && active.name) active.dispatchEvent(new Event('change'));
    ui.openItemId = null;
  });

  let resizeTimer;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (ui.view === 'matrix' && !$('#list-view').hidden) renderMatrix(); }, 150);
  });
}

async function start() {
  config = await api('GET', '/api/config');
  if (config.authRequired && !config.authed) {
    showScreen('login');
    return;
  }
  await refresh();
  render();
  connectLive();
}

bind();
start().catch((err) => toast(err.message));
