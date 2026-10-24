import unittest

from events import EventBus, PublishError


class HiddenEventBusTests(unittest.TestCase):
    def test_unsubscribe_is_idempotent(self):
        bus = EventBus()
        seen = []
        cancel = bus.subscribe("a", seen.append)
        cancel()
        cancel()
        self.assertEqual(bus.publish("a", 1), 0)
        self.assertEqual(seen, [])

    def test_wildcard_matching(self):
        bus = EventBus()
        seen = []
        bus.subscribe("order.*", seen.append)
        for event in ["order.created", "order.paid.late", "order", "orders.created"]:
            bus.publish(event, event)
        self.assertEqual(seen, ["order.created", "order.paid.late"])

    def test_publish_returns_count_and_keeps_subscription_order(self):
        bus = EventBus()
        calls = []
        bus.subscribe("order.*", lambda _: calls.append("wild-1"))
        bus.subscribe("order.paid", lambda _: calls.append("exact"))
        bus.subscribe("order.*", lambda _: calls.append("wild-2"))
        self.assertEqual(bus.publish("order.paid"), 3)
        self.assertEqual(calls, ["wild-1", "exact", "wild-2"])

    def test_same_handler_twice_and_cancel_only_one(self):
        bus = EventBus()
        seen = []
        bus.subscribe("a", seen.append)
        cancel_second = bus.subscribe("a", seen.append)
        self.assertEqual(bus.publish("a", 1), 2)
        cancel_second()
        self.assertEqual(bus.publish("a", 2), 1)
        self.assertEqual(seen, [1, 1, 2])

    def test_errors_are_collected_and_others_still_run(self):
        bus = EventBus()
        seen = []
        boom1, boom2 = RuntimeError("1"), KeyError("2")

        def fail1(_):
            raise boom1

        def fail2(_):
            raise boom2

        bus.subscribe("a", fail1)
        bus.subscribe("a", seen.append)
        bus.subscribe("a", fail2)
        with self.assertRaises(PublishError) as caught:
            bus.publish("a", "x")
        self.assertEqual(seen, ["x"])
        self.assertEqual(caught.exception.errors, [boom1, boom2])

    def test_changes_during_publish_apply_next_time(self):
        bus = EventBus()
        seen = []
        cancels = {}

        def first(_):
            seen.append("first")
            cancels["second"]()
            bus.subscribe("a", lambda _: seen.append("late"))

        bus.subscribe("a", first)
        cancels["second"] = bus.subscribe("a", lambda _: seen.append("second"))
        self.assertEqual(bus.publish("a"), 2)
        self.assertEqual(seen, ["first", "second"])
        seen.clear()
        bus.publish("a")
        self.assertEqual(seen, ["first", "late"])
