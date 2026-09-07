"""A deliberately small review fixture; run directly to verify its observable facts."""
import json

class RetryQueue:
    retry_delays_ms = (1000, 3000)

    def __init__(self):
        self.failed = {}

    def record_failure(self, key, value):
        self.failed[key] = value

    def retry_delay(self, attempt):
        return self.retry_delays_ms[attempt - 1]

if __name__ == '__main__':
    original = RetryQueue()
    original.record_failure('sample', 'synthetic task')
    restarted = RetryQueue()
    result = {
        'retry_limit': len(original.retry_delays_ms),
        'retry_delays_ms': list(original.retry_delays_ms),
        'failed_task_survives_new_instance': 'sample' in restarted.failed,
        'third_party_dependencies': 0,
    }
    assert result['retry_limit'] == 2
    assert result['retry_delays_ms'] == [1000, 3000]
    assert result['failed_task_survives_new_instance'] is False
    print(json.dumps(result, indent=2))
