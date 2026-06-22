# 🥬 식재료 최저가 추적기 (개인용)

watchlist에 등록한 식재료의 **실구매 최저가**를 네이버 쇼핑에서 받아오고,
**목표가 이하로 떨어지면 맥 알림**을 보내고 로컬 대시보드(`dashboard.html`)를 만든다.

크롤링이 아니라 **공식 API**라서 잘 안 깨진다. 키가 없으면 목업 모드로 화면만 먼저 볼 수 있다.

---

## 1. 바로 실행 (키 없이 — 목업)

```bash
cd ~/Downloads/식재료-최저가
python3 tracker.py
open dashboard.html
```

화면이 어떻게 나오는지 먼저 확인. 가격은 가짜다.

---

## 2. 네이버 쇼핑 API 키 발급 (실데이터 — 무료, 5분)

1. **네이버 개발자센터** 접속 → 네이버 로그인
   👉 https://developers.naver.com/main/
2. 상단 **Application → 애플리케이션 등록**
3. **애플리케이션 이름**: 아무거나 (예: `식재료최저가`)
4. **사용 API**: **검색** 체크
5. **비로그인 오픈 API 서비스 환경**: **WEB 설정** 추가
   → 웹 서비스 URL 칸에 `http://localhost` 입력
6. 등록하면 **Client ID / Client Secret** 발급됨
7. `config.json` 의 `naver.client_id`, `client_secret` 에 붙여넣기

> 일일 호출 한도(개인용으론 충분)가 있으니, watchlist는 수십 개 이내로.

다시 실행하면 자동으로 실데이터 모드로 바뀐다:

```bash
python3 tracker.py && open dashboard.html
```

---

## 3. (선택) KAMIS 시세 — "살 타이밍" 보조

도매/소매 평년 시세로 "지금이 비싼 철인지"를 보려면:

1. **농산물유통정보 KAMIS** → https://www.kamis.or.kr
2. 상단 **OpenAPI** 메뉴에서 신청 → **인증키(cert_key) + 요청자 id(cert_id)** 발급 (무료)
3. `config.json` 의 `kamis` 에 입력

> 시세 연동은 품목 코드 매핑이 필요해서 다음 단계로 둠. 지금은 비워둬도 정상 동작.

---

## 4. watchlist 바꾸기

`config.json` 의 `watchlist` 를 편집:

```json
{ "name": "표시 이름", "query": "네이버 검색어", "target_price": 목표가숫자 }
```

- `query` 는 구체적으로 (`양파` 보다 `양파 1kg` 가 정확).
- `target_price` 이하로 떨어지면 알림 + 🟢 표시.

---

## 5. (선택) 하루 1~2번 자동 실행

`crontab -e` 에 추가 (예: 매일 오전 9시, 오후 6시):

```cron
0 9,18 * * * cd ~/Downloads/식재료-최저가 && /usr/bin/python3 tracker.py
```

목표가 이하인 품목이 있으면 맥 알림센터로 뜬다.

---

## 파일

| 파일 | 역할 |
|---|---|
| `tracker.py` | 메인 스크립트 (조회 → 알림 → 대시보드) |
| `config.json` | API 키 + watchlist + 옵션 |
| `dashboard.html` | 실행할 때마다 새로 생성되는 결과 화면 |
