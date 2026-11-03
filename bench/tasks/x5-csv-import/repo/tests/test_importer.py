import unittest

from crm.importer import import_customers


class ImporterTests(unittest.TestCase):
    def test_basic_import(self):
        data = "email,name\na@example.com,에이\nnot-an-email,비\n".encode("utf-8")
        customers, errors = import_customers(data)
        self.assertEqual(customers, [{"email": "a@example.com", "name": "에이"}])
        self.assertEqual(errors, ["2행: 이메일 형식이 아닙니다"])
