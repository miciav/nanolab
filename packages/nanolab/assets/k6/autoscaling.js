import http from 'k6/http';
import { check, sleep } from 'k6';

const BASE_URL = __ENV.NANOFAAS_URL || 'http://localhost:30080';
const FN = __ENV.NANOFAAS_FUNCTION || __ENV.FUNCTION_NAME || 'word-stats-java';
// Think time between iterations. It decides how much concurrency a VU actually
// offers: by Little's law a closed-loop VU keeps S/(S+Z) of a request in flight,
// so with the 50ms default and a 2.5ms function, 25 VUs offer ~1.2 concurrent
// requests however many VUs are added. Runs that need to press against a
// concurrency limit set this to 0, which makes in-flight equal the VU count.
const THINK_SECONDS = Number(__ENV.K6_THINK_SECONDS ?? 0.05);

// The share of requests allowed to fail. Generous by default because scale-from-zero means the
// first wave hits cold starts; a saturation run raises it further, because shedding load is what
// that profile is built to do and a run that reports failure for succeeding at its purpose only
// teaches people to ignore red.
const MAX_FAILED_RATE = Number(__ENV.K6_MAX_FAILED_RATE ?? 0.30);

// End-to-end p95 budget, when the scenario states one. This is the latency a caller experiences —
// queue wait included — and so it is NOT the same quantity as the controller's service-time SLO:
// a governor can hold service time at its target while callers wait behind the limit it set. That
// difference is the point of checking here rather than trusting the controller's own view.
const MAX_P95_MS = __ENV.K6_MAX_P95_MS ? Number(__ENV.K6_MAX_P95_MS) : null;

function thresholds() {
    const limits = { http_req_failed: [`rate<${MAX_FAILED_RATE}`] };
    if (MAX_P95_MS !== null) {
        limits.http_req_duration = [`p(95)<${MAX_P95_MS}`];
    }
    return limits;
}

export const options = {
    // Load profile is injected by the workflow via `k6 run --stage ...` (see
    // K6Config in one_vm_loadtest_adapter.py); CLI flags override script
    // options, so it is deliberately NOT duplicated here.
    thresholds: thresholds(),
};

const TEXTS = [
    'The quick brown fox jumps over the lazy dog. The dog barked at the fox while the fox ran away quickly.',
    'Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.',
    'To be or not to be that is the question whether tis nobler in the mind to suffer the slings and arrows of outrageous fortune.',
    'It was the best of times it was the worst of times it was the age of wisdom it was the age of foolishness.',
];

export default function () {
    const text = TEXTS[Math.floor(Math.random() * TEXTS.length)];
    const payload = JSON.stringify({
        input: { text: text, topN: 5 },
    });

    const res = http.post(`${BASE_URL}/v1/functions/${FN}:invoke`, payload, {
        headers: { 'Content-Type': 'application/json' },
        timeout: '30s',
    });

    check(res, {
        'status is 200': (r) => r.status === 200,
    });

    if (THINK_SECONDS > 0) {
        sleep(THINK_SECONDS);
    }
}
