// Scoring helpers shared by the browser UI and the tests.
// Scores are 1–5 per dimension: impact (higher is better), effort and cost
// (higher is worse). Each person scores independently; "combined" averages them.

export const DIMENSIONS = ['impact', 'effort', 'cost'];
export const MIDPOINT = 3;

export const LABELS = {
  impact: ['Tiny', 'Small', 'Medium', 'Big', 'Huge'],
  effort: ['Minutes', 'An hour', 'A day', 'A weekend', 'Weeks'],
  cost: ['Free', 'Cheap', 'Moderate', 'Pricey', 'Expensive'],
};

/** Scores for one member, or averaged across everyone who scored ("combined"). */
export function scoresFor(item, view = 'combined') {
  if (view !== 'combined') {
    const own = item.scores?.[view] || {};
    return Object.fromEntries(DIMENSIONS.map((d) => [d, own[d] ?? null]));
  }
  const out = {};
  for (const dim of DIMENSIONS) {
    const values = Object.values(item.scores || {})
      .map((s) => s[dim])
      .filter((v) => Number.isFinite(v));
    out[dim] = values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  }
  return out;
}

/** Largest gap between two people's scores on any dimension (0 when < 2 scorers). */
export function disagreement(item) {
  let worst = 0;
  for (const dim of DIMENSIONS) {
    const values = Object.values(item.scores || {})
      .map((s) => s[dim])
      .filter((v) => Number.isFinite(v));
    if (values.length > 1) worst = Math.max(worst, Math.max(...values) - Math.min(...values));
  }
  return worst;
}

/** Members who have not yet given all three scores for an item. */
export function missingScorers(item, members) {
  return members.filter((m) => DIMENSIONS.some((d) => !Number.isFinite(item.scores?.[m]?.[d])));
}

/**
 * The "how hard is it" value for the horizontal axis.
 * axis: 'effort' | 'cost' | 'blend'. For 'blend', costWeight (0–1) sets how much
 * cost counts relative to effort; a missing cost falls back to effort alone.
 */
export function xValue(scores, axis = 'effort', costWeight = 0.5) {
  const { effort, cost } = scores;
  if (axis === 'effort') return effort;
  if (axis === 'cost') return cost;
  if (effort == null) return cost == null ? null : cost;
  if (cost == null) return effort;
  return effort * (1 - costWeight) + cost * costWeight;
}

/** Bang for buck: impact divided by the blended effort/cost. Range 0.2–5, null if unscored. */
export function priority(scores, costWeight = 0.5) {
  const x = xValue(scores, 'blend', costWeight);
  if (scores.impact == null || x == null) return null;
  return scores.impact / x;
}

export const QUADRANTS = {
  quickWin: { name: 'Quick wins', hint: 'Do these first' },
  bigProject: { name: 'Big projects', hint: 'Plan these together' },
  fillIn: { name: 'Fill-ins', hint: 'When you have a spare moment' },
  timeSink: { name: 'Time sinks', hint: 'Question or drop' },
};

export function quadrant(scores, axis = 'effort', costWeight = 0.5) {
  const x = xValue(scores, axis, costWeight);
  if (scores.impact == null || x == null) return null;
  const highImpact = scores.impact >= MIDPOINT;
  const easy = x < MIDPOINT;
  if (highImpact) return easy ? 'quickWin' : 'bigProject';
  return easy ? 'fillIn' : 'timeSink';
}

/** Sort comparator: undone first, then highest priority, then unscored last. */
export function rankItems(items, view, costWeight) {
  return items
    .map((item) => {
      const scores = scoresFor(item, view);
      return { item, scores, priority: priority(scores, costWeight) };
    })
    .sort((a, b) => {
      if (a.item.done !== b.item.done) return a.item.done ? 1 : -1;
      if (a.priority == null && b.priority == null) return a.item.createdAt.localeCompare(b.item.createdAt);
      if (a.priority == null) return 1;
      if (b.priority == null) return -1;
      return b.priority - a.priority || (b.scores.impact ?? 0) - (a.scores.impact ?? 0);
    });
}
