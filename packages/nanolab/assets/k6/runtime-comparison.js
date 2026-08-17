import http from 'k6/http';
import { check } from 'k6';

// Load that varies the way real traffic varies, so builds can be told apart.
//
// A flat rate compares steady-state throughput and nothing else, and that is the
// one regime where the four control-plane builds are hardest to distinguish: a
// JIT reaches its peak and stays there, and a native image has already reached
// its own. The differences live in the transitions — the first seconds after a
// step, the recovery after a burst, the moments a collector has to keep up while
// the arrival rate is still climbing.
//
// So the shape is: warm, climb, hold, spike, recover, climb higher, spike
// harder, drain. Every phase appears twice at different intensities, because a
// build that only struggles once has been unlucky, not slow.
//
// Open-loop (`ramping-arrival-rate`), not closed: arrivals are scheduled by the
// clock and do not wait for the system. Under a closed loop the generator holds
// back when the system slows, which hides exactly the degradation being compared
// — a slower build would simply receive less work and post similar latencies.
//
// `dropped_iterations` in the summary is the GENERATOR giving up because its VU
// pool was exhausted. It is not the platform refusing work and must never be
// added to the platform's rejections.
const BASE_URL = __ENV.NANOFAAS_URL || 'http://localhost:30080';
const FN_JAVA = __ENV.NANOFAAS_FUNCTION || 'word-stats-java';
const FN_JS = __ENV.NANOFAAS_NEIGHBOUR || 'word-stats-javascript';

const WARM_RPS = Number(__ENV.K6_WARM_RPS || 40);
const BASE_RPS = Number(__ENV.K6_BASE_RPS || 200);
const HIGH_RPS = Number(__ENV.K6_HIGH_RPS || 350);
const SPIKE_RPS = Number(__ENV.K6_SPIKE_RPS || 600);
const PEAK_RPS = Number(__ENV.K6_PEAK_RPS || 900);
const MAX_VUS = Number(__ENV.K6_MAX_VUS || 600);

// Node is single-threaded, so the JavaScript function saturates at a fraction of
// the Java one's rate. Offering both the same rate would put the JS function into
// permanent overload, and the run would then measure that function rather than the
// control plane in front of it. The scale keeps the *shape* identical — same
// ramps, same spikes, same instants — at an intensity the runtime can serve.
const JS_SCALE = Number(__ENV.K6_JS_SCALE || 0.35);

// One shape, expressed once. Both scenarios derive from it so that a spike hits
// both functions at the same second: staggering them would mix a co-tenancy
// question into a comparison that is not asking one.
const SHAPE = [
    ['30s', WARM_RPS],   // warm: give a JIT something to compile and a pool something to fill
    ['60s', BASE_RPS],   // climb
    ['60s', BASE_RPS],   // hold
    ['10s', SPIKE_RPS],  // step up sharply
    ['30s', SPIKE_RPS],  // hold the burst
    ['10s', BASE_RPS],   // step back down
    ['60s', BASE_RPS],   // recover: does latency return to where it was?
    ['60s', HIGH_RPS],   // climb higher
    ['45s', HIGH_RPS],   // hold
    ['10s', PEAK_RPS],   // the harder burst
    ['30s', PEAK_RPS],   // hold it
    ['45s', WARM_RPS],   // drain
];

const stages = (scale) =>
    SHAPE.map(([duration, target]) => ({
        duration,
        target: Math.max(1, Math.round(target * scale)),
    }));

const arrivals = (scale, exec) => ({
    executor: 'ramping-arrival-rate',
    startRate: Math.max(1, Math.round(WARM_RPS * scale)),
    timeUnit: '1s',
    preAllocatedVUs: MAX_VUS,
    maxVUs: MAX_VUS,
    stages: stages(scale),
    exec,
});

export const options = {
    scenarios: {
        java: arrivals(1, 'java'),
        javascript: arrivals(JS_SCALE, 'javascript'),
    },
    // No thresholds. Whether a build sheds requests under the peak is a result,
    // and gating on it would make the run report its own question as a failure.
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

export function java() {
    invoke(FN_JAVA);
}

export function javascript() {
    invoke(FN_JS);
}
