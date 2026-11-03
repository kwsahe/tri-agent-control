import unittest
from unittest.mock import patch

from resilience.retry import retry


def flaky(failures, error=ConnectionError):
    """처음 failures번은 error를 내고 그다음 "ok"를 돌려주는 함수."""
    state = {"calls": 0, "errors": []}

    def call():
        state["calls"] += 1
        if state["calls"] <= failures:
            exc = error(f"실패 {state['calls']}")
            state["errors"].append(exc)
            raise exc
        return "ok"

    return call, state


class HiddenRetryTests(unittest.TestCase):
    def test_recovers_and_backs_off(self):
        sleeps = []
        call, state = flaky(2)
        wrapped = retry(times=3, delay=0.1, backoff=2.0, sleep=sleeps.append)(call)
        self.assertEqual(wrapped(), "ok")
        self.assertEqual(state["calls"], 3)
        self.assertEqual(sleeps, [0.1, 0.2])

    def test_exhausted_raises_the_last_error_without_a_final_sleep(self):
        sleeps = []
        call, state = flaky(10)
        wrapped = retry(times=3, delay=1, sleep=sleeps.append)(call)
        with self.assertRaises(ConnectionError) as caught:
            wrapped()
        self.assertIs(caught.exception, state["errors"][-1])
        self.assertEqual(state["calls"], 3)
        self.assertEqual(len(sleeps), 2)

    def test_other_errors_are_not_retried(self):
        sleeps = []
        call, state = flaky(1, error=KeyError)
        wrapped = retry(times=5, exceptions=(ConnectionError,), sleep=sleeps.append)(call)
        with self.assertRaises(KeyError):
            wrapped()
        self.assertEqual((state["calls"], sleeps), (1, []))

    def test_delay_is_capped(self):
        sleeps = []
        call, _state = flaky(10)
        wrapped = retry(times=4, delay=1, backoff=10, max_delay=5, sleep=sleeps.append)(call)
        with self.assertRaises(ConnectionError):
            wrapped()
        self.assertEqual(sleeps, [1, 5, 5])

    def test_on_retry_sees_each_failed_attempt_before_sleeping(self):
        events = []
        call, state = flaky(2)
        wrapped = retry(times=3, sleep=lambda seconds: events.append(("sleep", seconds)),
                        on_retry=lambda attempt, exc: events.append(("retry", attempt, exc)))(call)
        wrapped()
        self.assertEqual(events, [("retry", 1, state["errors"][0]), ("sleep", 0.1),
                                  ("retry", 2, state["errors"][1]), ("sleep", 0.2)])

    def test_default_sleep_is_time_sleep_looked_up_at_call_time(self):
        call, _state = flaky(1)
        wrapped = retry(times=2, delay=0.5)(call)
        with patch("time.sleep") as fake_sleep:
            self.assertEqual(wrapped(), "ok")
        fake_sleep.assert_called_once_with(0.5)

    def test_single_attempt_never_sleeps(self):
        sleeps = []
        call, _state = flaky(1)
        with self.assertRaises(ConnectionError):
            retry(times=1, sleep=sleeps.append)(call)()
        self.assertEqual(sleeps, [])

    def test_metadata_is_kept(self):
        @retry()
        def fetch_user():
            """사용자를 가져온다."""

        self.assertEqual(fetch_user.__name__, "fetch_user")
        self.assertEqual(fetch_user.__doc__, "사용자를 가져온다.")

    def test_invalid_times(self):
        for times in [0, -1, 2.5, "3", True]:
            with self.subTest(times=times):
                with self.assertRaises(ValueError):
                    retry(times=times)
