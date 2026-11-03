import copy
import unittest

from appconfig.env import load
from appconfig.merge import merge

BASE = {"db": {"host": "localhost", "port": 5432, "options": ["a"]}, "debug": False, "name": "app"}


class HiddenMergeTests(unittest.TestCase):
    def test_inputs_are_never_changed(self):
        base, override = copy.deepcopy(BASE), {"db": {"port": 1}, "tags": ["x"]}
        before_base, before_override = copy.deepcopy(base), copy.deepcopy(override)
        result = merge(base, override)
        self.assertEqual(base, before_base)
        self.assertEqual(override, before_override)
        result["db"]["host"] = "changed"
        result["db"]["options"].append("b")
        result["tags"].append("y")
        self.assertEqual(base, before_base)
        self.assertEqual(override, before_override)

    def test_lists_are_replaced_not_joined(self):
        self.assertEqual(merge({"a": [1, 2]}, {"a": [3]}), {"a": [3]})

    def test_none_deletes_keys_even_when_nested(self):
        self.assertEqual(merge(BASE, {"db": {"options": None}, "debug": None}),
                         {"db": {"host": "localhost", "port": 5432}, "name": "app"})

    def test_dict_replaces_a_non_dict_and_back(self):
        self.assertEqual(merge({"a": 1}, {"a": {"b": 2}}), {"a": {"b": 2}})
        self.assertEqual(merge({"a": {"b": 2}}, {"a": 3}), {"a": 3})


class HiddenEnvTests(unittest.TestCase):
    def test_nested_keys_and_type_conversion(self):
        environ = {"APP__DB__PORT": "6543", "APP__DEBUG": "TRUE", "APP__NAME": "prod",
                   "APP__RATIO": "1.5", "APP__OFFSET": "-7", "HOME": "/root"}
        result = load(BASE, environ)
        self.assertEqual(result["db"], {"host": "localhost", "port": 6543, "options": ["a"]})
        self.assertIs(result["debug"], True)
        self.assertEqual(result["name"], "prod")
        self.assertEqual(result["ratio"], "1.5")
        self.assertEqual(result["offset"], -7)
        self.assertNotIn("home", result)

    def test_false_and_empty_values(self):
        result = load({"debug": True, "db": {"host": "x"}}, {"APP__DEBUG": "False", "APP__DB__HOST": ""})
        self.assertIs(result["debug"], False)
        self.assertEqual(result["db"], {})

    def test_malformed_keys_are_ignored(self):
        self.assertEqual(load({"a": 1}, {"APP__": "x", "APP__DB____PORT": "1", "APP____X": "2"}), {"a": 1})

    def test_load_does_not_change_base(self):
        base = copy.deepcopy(BASE)
        load(base, {"APP__DB__PORT": "1", "APP__DB__OPTIONS": ""})
        self.assertEqual(base, BASE)
