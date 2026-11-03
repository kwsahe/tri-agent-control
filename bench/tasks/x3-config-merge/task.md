appconfig 패키지를 아래 규칙대로 고치고 기능을 추가해.

1. merge(base, override)를 고친다.
   - dict는 재귀적으로 합친다. 리스트 같은 다른 값은 override 값으로 통째로 바꾼다 (리스트를 이어 붙이지 않는다).
   - override의 값이 None이면 결과에서 그 키를 지운다 (중첩된 곳에서도).
   - base와 override는 절대 바뀌면 안 된다. 결과에 든 dict·list도 입력과 공유하지 않는다 (결과를 고쳐도 입력이 바뀌지 않는다).
2. appconfig/env.py에 load(base, environ)를 추가한다. environ(dict)에서 APP__로 시작하는 키만 읽어 base 위에 덮어쓴 새 설정을 돌려준다.
   - APP__DB__PORT는 ["db"]["port"]가 된다 (키는 소문자로).
   - 값 변환: "true"/"false"(대소문자 무시)는 bool, 부호가 있을 수 있는 정수("42", "-7")는 int, 나머지는 문자열 그대로.
   - 값이 빈 문자열이면 그 키를 지운다 (merge의 None과 같다).
   - "APP__"처럼 이름이 비었거나 "APP__DB____PORT"처럼 빈 구간이 있는 키는 무시한다.
   - load도 base를 바꾸면 안 된다.
