numerals.py에 로마 숫자 변환 함수 두 개를 추가해: to_roman(n)과 from_roman(text).

- 1 ~ 3999만 지원한다. 범위 밖이거나 int가 아니면(bool 포함) ValueError.
- from_roman은 대문자 표준 표기만 받는다. "IIII", "IC", "MMMM", "VX", 소문자, 빈 문자열, 공백이나 로마 숫자가 아닌 문자가 섞인 문자열은 ValueError.
- 두 함수는 1~3999 전체에서 서로 역함수여야 한다.
- 이미 있는 ROMAN_SYMBOLS 표를 쓰면 된다.
