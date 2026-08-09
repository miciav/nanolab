import http from 'k6/http';
import { Rate } from 'k6/metrics';

const accepted = new Rate('sync_queue_accepted');
const rejected = new Rate('sync_queue_rejected');
const retryAfter = new Rate('sync_queue_retry_after');
const depthReason = new Rate('sync_queue_depth_reason');

export const options = {
    thresholds: {
        sync_queue_accepted: ['rate>0'],
        sync_queue_rejected: ['rate>0'],
        sync_queue_retry_after: ['rate==1'],
        sync_queue_depth_reason: ['rate==1'],
    },
};

export default function () {
    const response = http.post(
        `${__ENV.NANOFAAS_URL}/v1/functions/${__ENV.NANOFAAS_FUNCTION}:invoke`,
        __ENV.NANOFAAS_PAYLOAD,
        { headers: { 'Content-Type': 'application/json' }, timeout: '20s' },
    );
    accepted.add(response.status === 200);
    rejected.add(response.status === 429);
    if (response.status === 429) {
        retryAfter.add(response.headers['Retry-After'] === '2');
        depthReason.add(response.headers['X-Queue-Reject-Reason'] === 'depth');
    } else {
        retryAfter.add(true);
        depthReason.add(true);
    }
}
