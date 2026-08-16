import http from 'k6/http';
import { check } from 'k6';

// Two functions on one control plane, to see whether either governor reacts to
// the other's load.
//
// The windows overlap rather than nest, and both functions carry the SAME number
// of VUs, so each one gets a solo window and a shared one at identical load:
//
//   0-90s    A alone        -> A's solo limit
//   90-240s  A and B        -> both limits under co-tenancy
//   240-330s B alone        -> B's solo limit
//
// A first version had B only ever running alongside A, which made "together
// versus alone" answerable for A and unanswerable for B — and therefore made the
// question that matters, whether the two limits together fall short of what each
// reaches alone, not a question this run could ask.
//
// The phases are k6 scenarios rather than `--stage` flags because they have to
// be staggered, which a single global stage list cannot express. The plan
// therefore passes no stages for this script; CLI stages would override these.
const BASE_URL = __ENV.NANOFAAS_URL || 'http://localhost:30080';
const STEADY_FN = __ENV.NANOFAAS_FUNCTION || 'word-stats-java';
const BURST_FN = __ENV.NANOFAAS_NEIGHBOUR || 'word-stats-java-lite';

export const options = {
    scenarios: {
        steady: {
            executor: 'constant-vus',
            vus: 12,
            duration: '240s',
            exec: 'steady',
        },
        burst: {
            executor: 'constant-vus',
            vus: 12,
            startTime: '90s',
            duration: '240s',
            exec: 'burst',
        },
    },
    thresholds: {
        // The point of the run is what the limits do, not whether every request
        // survived: a governor that throttles hard will time some out.
        http_req_failed: ['rate<0.30'],
    },
};

const TEXTS = [
    'The quick brown fox jumps over the lazy dog. The dog barked at the fox while the fox ran away quickly.',
    'Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.',
    'To be or not to be that is the question whether tis nobler in the mind to suffer the slings and arrows of outrageous fortune.',
    'It was the best of times it was the worst of times it was the age of wisdom it was the age of foolishness.',
];

function invoke(fn) {
    const payload = JSON.stringify({
        input: { text: TEXTS[Math.floor(Math.random() * TEXTS.length)], topN: 5 },
    });
    const res = http.post(`${BASE_URL}/v1/functions/${fn}:invoke`, payload, {
        headers: { 'Content-Type': 'application/json' },
        timeout: '30s',
    });
    check(res, { 'status is 200': (r) => r.status === 200 });
}

export function steady() {
    invoke(STEADY_FN);
}

export function burst() {
    invoke(BURST_FN);
}
