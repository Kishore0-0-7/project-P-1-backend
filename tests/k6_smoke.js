/**
 * Read-only k6 smoke test for the OpsTriage API.
 *
 * Usage:
 *   k6 run tests/k6_smoke.js
 *   k6 run tests/k6_smoke.js --env BASE_URL=http://localhost:8000
 *   k6 run tests/k6_smoke.js --env VUS=10 --env DURATION=30s
 */
import http from 'k6/http';
import { check, sleep } from 'k6';

const baseUrl = (__ENV.BASE_URL || 'http://127.0.0.1:8000').replace(/\/$/, '');
const vus = Number(__ENV.VUS || 15);
const duration = __ENV.DURATION || '10s';

export const options = {
  vus,
  duration,
  thresholds: {
    checks: ['rate>0.99'],
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<500'],
  },
};

export default function () {
  const responses = http.batch([
    ['GET', `${baseUrl}/health`],
    ['GET', `${baseUrl}/`],
    ['GET', `${baseUrl}/openapi.json`],
  ]);

  check(responses[0], {
    'health returns 200': (response) => response.status === 200,
    'health reports ok': (response) => response.json('status') === 'ok',
  });

  check(responses[1], {
    'root returns 200': (response) => response.status === 200,
  });

  check(responses[2], {
    'OpenAPI returns 200': (response) => response.status === 200,
    'OpenAPI response is JSON': (response) =>
      response.headers['Content-Type']?.includes('application/json') ?? false,
    'OpenAPI document has version': (response) =>
      typeof response.json('openapi') === 'string',
  });

  sleep(0.2);
}
