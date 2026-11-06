import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))

from api.debounce_api import Debouncer


class FakeClock:
    """Minimal after/after_cancel with manual time."""

    def __init__(self):
        self.now = 0
        self.jobs = {}
        self.next_id = 1

    def after(self, delay, fn):
        job = self.next_id
        self.next_id += 1
        self.jobs[job] = (self.now + delay, fn)
        return job

    def cancel(self, job):
        self.jobs.pop(job)  # like Tk, unknown ids are an error the debouncer must tolerate

    def advance(self, ms):
        self.now += ms
        for job, (due, fn) in sorted(self.jobs.items(), key=lambda item: item[1][0]):
            if due <= self.now and job in self.jobs:
                del self.jobs[job]
                fn()


class DebouncerTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.calls = []
        self.debouncer = Debouncer(self.clock.after, self.clock.cancel, 400, lambda: self.calls.append(self.clock.now))

    def test_burst_of_triggers_runs_once_after_the_last_one(self):
        for _ in range(5):
            self.debouncer.trigger()
            self.clock.advance(100)
        self.assertEqual(self.calls, [])
        self.clock.advance(300)
        self.assertEqual(self.calls, [800])
        self.clock.advance(1000)
        self.assertEqual(len(self.calls), 1)

    def test_can_trigger_again_after_firing(self):
        self.debouncer.trigger()
        self.clock.advance(400)
        self.debouncer.trigger()
        self.clock.advance(400)
        self.assertEqual(len(self.calls), 2)
        self.assertFalse(self.debouncer.pending)

    def test_cancel_prevents_the_callback_and_is_safe_twice(self):
        self.debouncer.trigger()
        self.assertTrue(self.debouncer.pending)
        self.debouncer.cancel()
        self.debouncer.cancel()
        self.clock.advance(1000)
        self.assertEqual(self.calls, [])

    def test_cancel_errors_from_the_toolkit_are_ignored(self):
        debouncer = Debouncer(lambda delay, fn: 7, lambda job: 1 / 0, 10, lambda: None)
        debouncer.trigger()
        debouncer.trigger()  # re-trigger cancels the old job; the error must not escape


if __name__ == "__main__":
    unittest.main()
