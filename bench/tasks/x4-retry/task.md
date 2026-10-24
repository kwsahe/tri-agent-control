resilience/retry.py의 retry 데코레이터가 "다 실패해도 오류 없이 None을 돌려준다", "괜히 오래 기다린다"는 신고가 들어왔어. 아래 규칙대로 고쳐줘.

인자: retry(times=3, exceptions=(Exception,), delay=0.1, backoff=2.0, max_delay=10.0, sleep=None, on_retry=None)

- times는 첫 시도를 포함한 총 시도 횟수다. int가 아니거나(bool 포함) 1보다 작으면 데코레이터를 만들 때 ValueError.
- exceptions에 든 예외만 다시 시도한다. 다른 예외는 기다리지 않고 바로 그대로 올린다.
- 모든 시도가 실패하면 마지막 예외를 그대로 올린다.
- n번째 실패 뒤에는 delay × backoff^(n-1)초를 기다리되 max_delay를 넘지 않는다. 마지막 시도가 실패한 뒤에는 기다리지 않는다.
- sleep은 기다리는 함수다. None이면 호출할 때마다 time.sleep을 쓴다 (테스트에서 가짜 sleep을 넣을 수 있게).
- on_retry(attempt, exc)는 다시 시도하기 직전(기다리기 전)에 부른다. attempt는 방금 실패한 시도 번호(1부터)다.
- 감싼 함수의 이름과 docstring을 유지한다.
