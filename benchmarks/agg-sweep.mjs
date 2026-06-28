// Aggregate the 3x sweep reps into median-per-VU -> results/sweep/summary.json
import { readFileSync, writeFileSync, readdirSync } from 'node:fs';
const DIR = 'results/sweep';
const load = f => { const c = readFileSync(`${DIR}/${f}`, 'utf8'); return JSON.parse(c.slice(c.indexOf('{'), c.lastIndexOf('}') + 1)); };
const median = a => { const s = [...a].sort((x, y) => x - y); const m = s.length >> 1; return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2; };

const files = readdirSync(DIR).filter(f => /^r\d+_vu\d+\.json$/.test(f));
const byVU = {};
for (const f of files) {
  const v = +f.match(/vu(\d+)/)[1];
  const m = load(f).metrics, d = m.http_req_duration.values;
  (byVU[v] ||= []).push({
    rps: m.http_reqs.values.rate, p50: d.med, p95: d['p(95)'], p99: d['p(99)'],
    max: d.max, fail: m.http_req_failed.values.rate * 100,
  });
}
const rows = Object.keys(byVU).map(Number).sort((a, b) => a - b).map(v => {
  const reps = byVU[v], pick = k => reps.map(r => r[k]);
  return {
    vus: v, reps: reps.length,
    rps: Math.round(median(pick('rps'))),
    p50: +median(pick('p50')).toFixed(1), p95: +median(pick('p95')).toFixed(1),
    p99: +median(pick('p99')).toFixed(1), max: +median(pick('max')).toFixed(0),
    fail: +median(pick('fail')).toFixed(2),
  };
});
writeFileSync(`${DIR}/summary.json`, JSON.stringify(rows, null, 2));
console.log('VU\treps\trps\tp50\tp95\tp99\tmax\tfail%');
rows.forEach(r => console.log(`${r.vus}\t${r.reps}\t${r.rps}\t${r.p50}\t${r.p95}\t${r.p99}\t${r.max}\t${r.fail}`));
