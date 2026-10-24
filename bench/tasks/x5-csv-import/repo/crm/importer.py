"""고객 CSV 가져오기."""

import csv
import io

from .dedupe import dedupe
from .validate import validate_row


def import_customers(data: bytes) -> tuple[list[dict], list[str]]:
    """CSV 바이트를 읽어 (고객 목록, 오류 목록)을 돌려준다.

    오류는 "N행: 사유" 형식이고, N은 헤더 다음 줄부터 1이다.
    """
    text = data.decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    if "email" not in (reader.fieldnames or []):
        raise ValueError("email 열이 없습니다")
    customers, errors = [], []
    for number, row in enumerate(reader, start=1):
        problem = validate_row(row)
        if problem:
            errors.append(f"{number}행: {problem}")
            continue
        customers.append({"email": row["email"], "name": row.get("name", "")})
    return dedupe(customers), errors
