import unittest

from events import EventBus


class EventBusTests(unittest.TestCase):
    def test_publish_calls_subscribers(self):
        bus = EventBus()
        seen = []
        bus.subscribe("order.created", seen.append)
        bus.publish("order.created", {"id": 1})
        self.assertEqual(seen, [{"id": 1}])
