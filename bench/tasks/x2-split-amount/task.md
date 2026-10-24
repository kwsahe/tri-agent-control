나눈 금액을 다시 더하면 원래 금액과 1~2원씩 안 맞는다는 신고가 들어왔어. 아래 분배 규칙대로 고쳐줘.

- 금액을 나누는 모든 곳(billing 패키지의 allocate, Invoice.split_by_people, settle)이 같은 규칙을 따라야 한다.
- 결과를 더하면 항상 total과 정확히 같다.
- 각 몫은 total × 비중 ÷ 비중 합을 내림한 값에서 시작한다. 남은 원은 버려진 소수 부분이 큰 순서로 1원씩 더 준다. 소수 부분이 같으면 앞쪽(먼저 나온 사람)이 먼저 받는다.
- 비중 0은 0원을 받는다. 비중이 음수이거나 비중 합이 0이면 ValueError. total이 음수이면 ValueError.
- settle은 딕셔너리 순서를 "앞쪽"으로 보고 같은 규칙을 쓴다.
- split_by_people에서 people이 1보다 작으면 ValueError.
- 계산 중에 부동소수 오차가 생기면 안 된다.
