import http from 'k6/http';
import { check } from 'k6';

// Two functions, two queues, deliberately variable load.
//
// The earlier co-tenancy script held 12 VUs per function against a limit of 8
// and a queue of 100, so the buffer never held more than about two requests:
// the run measured the limit and nothing about the cost of imposing it. A
// concurrency limit does not delete work, it moves it into the queue, so a run
// that never fills the queue cannot say what the limit charged the caller.
//
// Closed-loop VUs with no think time make the buffer directly steerable. Each
// VU holds exactly one request, in service or queued, so
//
//     queue depth = min(VUs, limit + queue) - limit
//
// which means the peak VU count sets the fill fraction and the controller's
// limit is read straight off the depth: a controller that grants more keeps a
// SHORTER queue at the same offered load. An open-loop arrival rate cannot do
// this — the queue there either drains or runs to full, with no tunable middle,
// and the capacity you would have to aim at is the very thing under test.
//
// PEAK is set just under limit + queue (8 + 100), so a controller holding its
// ceiling brushes the top of the buffer without refusing anything, while one
// that collapses to a small limit overflows. Rejections are then a verdict on
// the controller rather than a property of the load.
const BASE_URL = __ENV.NANOFAAS_URL || 'http://localhost:30080';
const FN_A = __ENV.NANOFAAS_FUNCTION || 'word-stats-java';
const FN_B = __ENV.NANOFAAS_NEIGHBOUR || 'word-stats-java-lite';
const PEAK = Number(__ENV.K6_PEAK_VUS || 105);
const TROUGH = Number(__ENV.K6_TROUGH_VUS || 15);

// Three phases of two minutes, each answering one question.
//
//   0-120s   A alone, sawtooth        - what one function does with the whole machine
//   120-240s A and B in ANTIPHASE     - can capacity follow the load between them?
//   240-360s A and B IN PHASE         - what happens when both want it at once
//
// The antiphase window is the one a per-function controller cannot get right:
// while B is idle its share is there to be lent, and only an allocator that
// sees both functions can lend it.
const A_STAGES = [
    // Phase 1: alone, three sawteeth.
    { duration: '20s', target: PEAK },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: TROUGH },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: TROUGH },
    // Phase 2: antiphase - A peaks first, then holds low while B peaks.
    { duration: '20s', target: PEAK },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: TROUGH },
    { duration: '60s', target: TROUGH },
    // Phase 3: in phase - both climb together.
    { duration: '20s', target: PEAK },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: TROUGH },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: TROUGH },
];

// B starts at 120s. Its first peak lands in A's trough (antiphase), its later
// peaks land on A's peaks (in phase).
const B_STAGES = [
    { duration: '40s', target: TROUGH },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: TROUGH },
    { duration: '20s', target: TROUGH },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: TROUGH },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: PEAK },
    { duration: '20s', target: TROUGH },
];

export const options = {
    scenarios: {
        steady: {
            executor: 'ramping-vus',
            startVUs: TROUGH,
            stages: A_STAGES,
            exec: 'steady',
            gracefulRampDown: '0s',
        },
        burst: {
            executor: 'ramping-vus',
            startVUs: TROUGH,
            stages: B_STAGES,
            startTime: '120s',
            exec: 'burst',
            gracefulRampDown: '0s',
        },
    },
    // No threshold on the failure rate: whether the queue overflows is the
    // result being measured, so gating on it would make the run report its own
    // question as an error.
    thresholds: {},
};

const TEXTS = [
    'The quick brown fox jumps over the lazy dog. The dog barked at the fox while the fox ran away quickly.',
    'Lorem ipsum dolor sit amet consectetur adipiscing elit sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.',
    'To be or not to be that is the question whether tis nobler in the mind to suffer the slings and arrows of outrageous fortune.',
    'It was the best of times it was the worst of times it was the age of wisdom it was the age of foolishness.',
];

function invoke(functionName) {
    const body = JSON.stringify({
        input: { text: TEXTS[__ITER % TEXTS.length], topN: 5 },
    });
    const response = http.post(
        `${BASE_URL}/v1/functions/${functionName}:invoke`,
        body,
        {
            headers: { 'Content-Type': 'application/json' },
            timeout: '30s',
            tags: { fn: functionName },
        },
    );
    // 429 is the queue refusing work, which this run expects to see and counts
    // rather than treats as a broken request.
    check(response, {
        'served': (r) => r.status === 200,
        'rejected': (r) => r.status === 429 || r.status === 503,
    });
}

export function steady() {
    invoke(FN_A);
}

export function burst() {
    invoke(FN_B);
}
