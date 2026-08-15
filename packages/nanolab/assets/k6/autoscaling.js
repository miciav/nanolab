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

export const options = {
    // Load profile is injected by the workflow via `k6 run --stage ...` (see
    // K6Config in one_vm_loadtest_adapter.py); CLI flags override script
    // options, so it is deliberately NOT duplicated here.
    thresholds: {
        // Generous on purpose: scale-from-zero means the first wave of requests
        // hits cold starts and may time out before replicas come up.
        http_req_failed: ['rate<0.30'],
    },
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
