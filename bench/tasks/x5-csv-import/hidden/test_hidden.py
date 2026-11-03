import unittest

from crm.dedupe import dedupe
from crm.importer import import_customers


class HiddenImporterTests(unittest.TestCase):
    def test_excel_utf8_bom_file(self):
        data = "﻿email,name\nkim@example.com,김\n".encode("utf-8")
        customers, errors = import_customers(data)
        self.assertEqual(customers, [{"email": "kim@example.com", "name": "김"}])
        self.assertEqual(errors, [])

    def test_header_names_ignore_case_and_spaces(self):
        customers, _errors = import_customers(" Email , Name \nlee@example.com, 이 \n".encode("utf-8"))
        self.assertEqual(customers, [{"email": "lee@example.com", "name": "이"}])

    def test_duplicates_ignore_case_and_spaces_and_keep_the_first(self):
        data = "email,name\n Kim@Example.com ,첫째\nkim@example.com,둘째\nPARK@example.com,박\n".encode("utf-8")
        customers, _errors = import_customers(data)
        self.assertEqual(customers, [{"email": "kim@example.com", "name": "첫째"},
                                     {"email": "park@example.com", "name": "박"}])

    def test_validation_uses_the_cleaned_email(self):
        customers, errors = import_customers("email\n  a@b.com  \n b @c.com\n".encode("utf-8"))
        self.assertEqual([c["email"] for c in customers], ["a@b.com"])
        self.assertEqual(errors, ["2행: 이메일 형식이 아닙니다"])

    def test_error_numbering_is_unchanged(self):
        data = "email,name\nbad,1\nok@example.com,2\n\nworse,3\n".encode("utf-8")
        _customers, errors = import_customers(data)
        self.assertEqual(errors, ["1행: 이메일 형식이 아닙니다", "3행: 이메일 형식이 아닙니다"])

    def test_missing_email_column_still_fails(self):
        with self.assertRaises(ValueError):
            import_customers("name\n김\n".encode("utf-8"))


class HiddenDedupeTests(unittest.TestCase):
    def test_dedupe_alone_uses_the_same_rule(self):
        customers = [{"email": "A@x.com", "name": "1"}, {"email": " a@x.com", "name": "2"},
                     {"email": "b@x.com", "name": "3"}]
        self.assertEqual([c["name"] for c in dedupe(customers)], ["1", "3"])
