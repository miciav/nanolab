import http from 'k6/http';
import { check } from 'k6';

// Arrivals that do not wait for the system, so the queue can grow on its own.
//
// The burst profile is closed-loop: each VU holds exactly one request, so the
// number in the system is pinned at the VU count and queue depth is simply
// VUs - limit. That makes the offered load a constant the controller cannot
// influence, and it caps what any concurrency limit can achieve: raising the
// limit from 4 to 8 shortened the queue by 4% and nothing else, because the
// generator was holding the rest. Two controllers deciding from completely
// different signals produced end-to-end p95 within 2% of each other, which was
// a property of the harness rather than a finding about them.
//
// Here arrivals are scheduled by the clock. If the system slows, requests keep
// coming, the queue grows because demand exceeded capacity rather than because a
// generator was told to hold more, and the wait becomes something a limit can
// actually change. This is the condition a sojourn-driven controller exists for,
// and the one under which it can be shown to be right or wrong.
//
// The cost is that overload is real: `preAllocatedVUs` bounds the concurrency k6
// itself can hold, and beyond it k6 drops iterations rather than sending them.
// Dropped iterations are the GENERATOR giving up, not the platform refusing, and
// the two must not be added together — `dropped_iterations` is reported
// separately for exactly that reason.
const BASE_URL = __ENV.NANOFAAS_URL || 'http://localhost:30080';
const FN_A = __ENV.NANOFAAS_FUNCTION || 'word-stats-java';
const FN_B = __ENV.NANOFAAS_NEIGHBOUR || 'word-stats-java-lite';
const PEAK_RPS = Number(__ENV.K6_PEAK_RPS || 1600);
const TROUGH_RPS = Number(__ENV.K6_TROUGH_RPS || 300);
const MAX_VUS = Number(__ENV.K6_MAX_VUS || 400);

// The same three windows the closed-loop profile uses, so the two are readable
// against each other: one function alone, then antiphase, then in phase.
const A_STAGES = [
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: TROUGH_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: TROUGH_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: TROUGH_RPS },
    { duration: '60s', target: TROUGH_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: TROUGH_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: TROUGH_RPS },
];

const B_STAGES = [
    { duration: '40s', target: TROUGH_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: TROUGH_RPS },
    { duration: '20s', target: TROUGH_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: TROUGH_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: PEAK_RPS },
    { duration: '20s', target: TROUGH_RPS },
];

const arrivals = (stages, startTime, exec) => ({
    executor: 'ramping-arrival-rate',
    startRate: TROUGH_RPS,
    timeUnit: '1s',
    preAllocatedVUs: MAX_VUS,
    maxVUs: MAX_VUS,
    stages,
    ...(startTime ? { startTime } : {}),
    exec,
});

export const options = {
    scenarios: {
        steady: arrivals(A_STAGES, null, 'steady'),
        burst: arrivals(B_STAGES, '120s', 'burst'),
    },
    // No threshold on the failure rate: whether the queue overflows is the result
    // being measured, so gating on it would make the run report its own question
    // as an error.
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
    check(response, {
        'served': (r) => r.status === 200,
        'refused': (r) => r.status === 429 || r.status === 503,
    });
}

export function steady() {
    invoke(FN_A);
}

export function burst() {
    invoke(FN_B);
}
