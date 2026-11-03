events 패키지의 EventBus를 확장해.

1. subscribe가 구독 취소 함수를 돌려준다. 그 함수를 여러 번 불러도 오류가 없다.
2. "order.*"처럼 끝이 ".*"인 와일드카드 구독을 지원한다.
   - "order.*"는 "order.created", "order.paid.late"에 반응한다.
   - "order"나 "orders.created"에는 반응하지 않는다.
3. publish는 호출한 핸들러 수를 돌려준다.
4. 핸들러는 구독한 순서대로 호출한다. 정확한 이름 구독과 와일드카드 구독이 섞여 있어도 구독한 순서를 따른다.
5. 같은 핸들러를 두 번 구독하면 두 번 호출된다. 취소 함수는 자기 구독 하나만 취소한다.
6. 한 핸들러가 예외를 내도 나머지 핸들러는 계속 호출한다. 다 부른 뒤, 예외가 있었으면 PublishError를 낸다.
   - PublishError는 events/errors.py에 만들고, `from events import PublishError`로 가져올 수 있어야 한다.
   - errors 속성에 발생한 예외들을 순서대로 담는다.
7. 핸들러 안에서 구독하거나 취소해도 지금 진행 중인 publish에는 영향이 없다. publish를 시작할 때의 구독 목록을 쓴다.
