import { test } from 'node:test';
import assert from 'node:assert/strict';
import { scoresFor, disagreement, missingScorers, xValue, priority, quadrant, rankItems } from '../public/scoring.js';

const item = (scores, extra = {}) => ({ id: 'x', title: 't', done: false, createdAt: '2026-01-01', scores, ...extra });

test('scoresFor averages across members and reads a single member', () => {
  const it = item({ A: { impact: 5, effort: 2, cost: 1 }, B: { impact: 3, effort: 4 } });
  assert.deepEqual(scoresFor(it, 'combined'), { impact: 4, effort: 3, cost: 1 });
  assert.deepEqual(scoresFor(it, 'B'), { impact: 3, effort: 4, cost: null });
  assert.deepEqual(scoresFor(it, 'C'), { impact: null, effort: null, cost: null });
});

test('disagreement is the largest per-dimension gap', () => {
  assert.equal(disagreement(item({ A: { impact: 5, effort: 2 } })), 0);
  assert.equal(disagreement(item({ A: { impact: 5, effort: 2 }, B: { impact: 4, effort: 5 } })), 3);
});

test('missingScorers lists members without all three scores', () => {
  const it = item({ A: { impact: 5, effort: 2, cost: 1 }, B: { impact: 3 } });
  assert.deepEqual(missingScorers(it, ['A', 'B', 'C']), ['B', 'C']);
});

test('xValue blends effort and cost by weight, falling back when one is missing', () => {
  assert.equal(xValue({ effort: 2, cost: 4 }, 'effort'), 2);
  assert.equal(xValue({ effort: 2, cost: 4 }, 'cost'), 4);
  assert.equal(xValue({ effort: 2, cost: 4 }, 'blend', 0.5), 3);
  assert.equal(xValue({ effort: 2, cost: 4 }, 'blend', 0), 2);
  assert.equal(xValue({ effort: 2, cost: null }, 'blend', 0.5), 2);
  assert.equal(xValue({ effort: null, cost: 4 }, 'blend', 0.5), 4);
  assert.equal(xValue({ effort: null, cost: null }, 'blend', 0.5), null);
});

test('priority is impact over blended effort/cost', () => {
  assert.equal(priority({ impact: 5, effort: 1, cost: 1 }), 5);
  assert.equal(priority({ impact: 4, effort: 2, cost: 6 }, 0.5), 1);
  assert.equal(priority({ impact: null, effort: 1, cost: 1 }), null);
});

test('cost changes the quadrant when it is on the axis', () => {
  const s = { impact: 5, effort: 1, cost: 5 };
  assert.equal(quadrant(s, 'effort'), 'quickWin');
  assert.equal(quadrant(s, 'cost'), 'bigProject');
  assert.equal(quadrant({ impact: 1, effort: 1, cost: 1 }, 'effort'), 'fillIn');
  assert.equal(quadrant({ impact: 2, effort: 5, cost: 1 }, 'effort'), 'timeSink');
});

test('rankItems puts open, high-priority items first and unscored last', () => {
  const items = [
    item({}, { id: 'unscored', createdAt: '2026-01-01' }),
    item({ A: { impact: 2, effort: 4, cost: 4 } }, { id: 'low' }),
    item({ A: { impact: 5, effort: 1, cost: 1 } }, { id: 'done', done: true }),
    item({ A: { impact: 5, effort: 2, cost: 1 } }, { id: 'high' }),
  ];
  assert.deepEqual(rankItems(items, 'combined', 0.5).map((r) => r.item.id), ['high', 'low', 'unscored', 'done']);
});
