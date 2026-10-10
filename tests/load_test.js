/**
 * k6 load test for OpsTriage API
 *
 * Run with:
 *   k6 run tests/load_test.js --env ORG_ID=<your-org-uuid>
 *
 * Targets:
 *   - p95 latency < 200 ms under 100 VUs
 *   - Error rate < 1%
 *   - Cache hit rate > 80% on repeated GETs
 *
 * Stages:
 *   1. Ramp up to 50 VUs over 30s
 *   2. Hold at 100 VUs for 2 minutes
 *   3. Ramp down
 */
import http from 'k6/http';
import { check, sleep } from 'k6';
import { Rate, Trend, Counter } from 'k6/metrics';

// ── Custom metrics ────────────────────────────────────────────────────────────
const errorRate = new Rate('error_rate');
const cacheHits = new Counter('cache_hits');
const cacheMisses = new Counter('cache_misses');
const findingsLatency = new Trend('findings_latency', true);

// ── Config ────────────────────────────────────────────────────────────────────
const BASE_URL = __ENV.BASE_URL || 'http://localhost:80';
const ORG_ID   = __ENV.ORG_ID   || 'your-org-uuid-here';

export const options = {
  stages: [
    { duration: '30s', target: 50  },   // ramp up
    { duration: '2m',  target: 100 },   // steady state – 100 concurrent users
    { duration: '30s', target: 0   },   // ramp down
  ],
  thresholds: {
    http_req_duration:         ['p(95)<200', 'p(99)<500'],
    http_req_failed:           ['rate<0.01'],   // < 1% errors
    error_rate:                ['rate<0.01'],
    findings_latency:          ['p(95)<150'],
  },
};

// ── Scenario ──────────────────────────────────────────────────────────────────
export default function () {
  const params = { headers: { 'Content-Type': 'application/json' } };

  // 1. List findings (heavily cached endpoint)
  const findingsRes = http.get(
    `${BASE_URL}/api/v1/findings?org_id=${ORG_ID}&limit=20`,
    params,
  );
  findingsLatency.add(findingsRes.timings.duration);

  const findingsOk = check(findingsRes, {
    'findings 200': (r) => r.status === 200,
    'findings body is array': (r) => Array.isArray(r.json()),
  });
  errorRate.add(!findingsOk);

  // Track Nginx cache hits
  const cacheHeader = findingsRes.headers['X-Cache-Status'];
  if (cacheHeader === 'HIT')  cacheHits.add(1);
  if (cacheHeader === 'MISS') cacheMisses.add(1);

  // 2. Create a new incident (write path)
  const incidentPayload = JSON.stringify({
    title: `Load test incident ${__VU}-${__ITER}`,
    priority: Math.ceil(Math.random() * 5),
    impact: Math.ceil(Math.random() * 5),
    urgency: Math.ceil(Math.random() * 5),
  });
  const incidentRes = http.post(
    `${BASE_URL}/api/v1/incidents?org_id=${ORG_ID}`,
    incidentPayload,
    params,
  );
  check(incidentRes, {
    'incident created 201': (r) => r.status === 201,
    'incident has priority_score': (r) => r.json('priority_score') !== undefined,
  });
  errorRate.add(incidentRes.status !== 201);

  // 3. Ingest a batch of sensor events (high-throughput path)
  const events = Array.from({ length: 10 }, (_, i) => ({
    source: 'k6-load-sensor',
    event_type: 'telemetry',
    occurred_at: new Date().toISOString(),
    payload: { cpu_util: 50 + i, mem_pct: 60 + i },
  }));
  const ingestRes = http.post(
    `${BASE_URL}/api/v1/ingest/events/batch?org_id=${ORG_ID}`,
    JSON.stringify({ events }),
    params,
  );
  check(ingestRes, {
    'ingest batch 202': (r) => r.status === 202,
    'all events accepted': (r) => r.json('accepted') === 10,
  });
  errorRate.add(ingestRes.status !== 202);

  // 4. Health probe
  const healthRes = http.get(`${BASE_URL}/health`);
  check(healthRes, { 'health ok': (r) => r.status === 200 });

  sleep(0.5);  // think time between iterations
}

export function handleSummary(data) {
  const cacheHitTotal = data.metrics.cache_hits?.values?.count ?? 0;
  const cacheMissTotal = data.metrics.cache_misses?.values?.count ?? 0;
  const totalCacheRequests = cacheHitTotal + cacheMissTotal;
  const hitRate = totalCacheRequests > 0
    ? ((cacheHitTotal / totalCacheRequests) * 100).toFixed(1)
    : 'N/A';

  return {
    stdout: `
========================================
  OpsTriage Load Test Summary
========================================
  p50  latency: ${data.metrics.http_req_duration.values['p(50)'].toFixed(1)} ms
  p95  latency: ${data.metrics.http_req_duration.values['p(95)'].toFixed(1)} ms
  p99  latency: ${data.metrics.http_req_duration.values['p(99)'].toFixed(1)} ms
  Error rate:   ${(data.metrics.error_rate.values.rate * 100).toFixed(2)}%
  Cache hits:   ${cacheHitTotal} / ${totalCacheRequests} (${hitRate}%)
  Total reqs:   ${data.metrics.http_reqs.values.count}
========================================
`,
  };
}
