throttle/bucket.py를 새로 만들고 TokenBucketLimiter 클래스를 추가해 (토큰 버킷 방식 요청 제한).

- TokenBucketLimiter(capacity, refill_per_second, clock=None)
  - capacity와 refill_per_second는 양수(int 또는 float, bool은 안 됨)여야 한다. 아니면 ValueError.
  - clock은 현재 시각(초)을 돌려주는 함수다. None이면 throttle.clock.monotonic을 쓴다.
- allow(key, cost=1) → bool
  - 키마다 버킷이 따로 있다. 처음 보는 키는 가득 찬 상태(capacity)에서 시작한다.
  - 마지막으로 그 키를 본 뒤 흐른 시간 × refill_per_second만큼 토큰을 채우되 capacity를 넘지 않는다. 소수 토큰도 쌓인다.
  - 토큰이 cost 이상이면 cost만큼 빼고 True를 돌려준다. 모자라면 아무것도 빼지 않고 False를 돌려준다.
  - 부동소수 오차 때문에 거의 다 찬 토큰이 아주 조금 모자라 거절되면 안 된다. 차이가 1e-9 이하면 통과로 본다.
- retry_after(key, cost=1) → float
  - 지금부터 몇 초 뒤에 allow(key, cost)가 통과하는지 돌려준다. 지금 통과할 수 있으면 0.0이다. 토큰은 쓰지 않는다.
- cost는 양수여야 하고 capacity보다 크면 안 된다 (영원히 통과할 수 없다). 어기면 allow와 retry_after 모두 ValueError.
- 시계가 뒤로 가면 흐른 시간을 0으로 보고, 그 키의 기준 시각을 새 시각으로 바꾼다.
