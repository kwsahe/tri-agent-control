"""고객 CSV 가져오기."""

import csv
import io

from .dedupe import dedupe, normalize_email
from .validate import validate_row


def import_customers(data: bytes) -> tuple[list[dict], list[str]]:
    """CSV 바이트를 읽어 (고객 목록, 오류 목록)을 돌려준다.

    오류는 "N행: 사유" 형식이고, N은 헤더 다음 줄부터 1이다.
    """
    text = data.decode("utf-8-sig")   # 엑셀의 "CSV UTF-8"은 맨 앞에 BOM을 붙인다
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames:
        reader.fieldnames = [name.strip().lower() for name in reader.fieldnames]
    if "email" not in (reader.fieldnames or []):
        raise ValueError("email 열이 없습니다")
    customers, errors = [], []
    for number, row in enumerate(reader, start=1):
        row = {**row, "email": normalize_email(row.get("email") or "")}
        problem = validate_row(row)
        if problem:
            errors.append(f"{number}행: {problem}")
            continue
        customers.append({"email": row["email"], "name": (row.get("name") or "").strip()})
    return dedupe(customers), errors
