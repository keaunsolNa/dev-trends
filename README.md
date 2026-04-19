# dev-trends

매일 08:00 KST에 Reddit · Stack Overflow · GitHub Discussions에서 가장 반응 많은 글 5개를 수집, 한글 번역 후 Slack에 전송하고 `reports/YYYY-MM-DD.md`로 커밋합니다.

## 구성

- `fetch_trends.py` — 수집·점수화·번역·MD 생성·Slack 전송
- `.github/workflows/daily-trends.yml` — cron + 커밋 워크플로
- `reports/` — 일자별 MD 리포트 (자동 생성)
- `requirements.txt` — `requests`, `deep-translator`

## 초기 셋업 (한 번만)

### 1. 레포 생성 & 파일 업로드
비공개 레포 권장. 이 4개 파일만 커밋하면 됩니다.

### 2. GitHub Actions Secrets 등록
레포 → Settings → Secrets and variables → Actions → New repository secret.

| Secret | 필수 | 발급처 |
|---|---|---|
| `SLACK_WEBHOOK_URL` | ✅ | Slack → Your Apps → Incoming Webhooks |
| `DEEPL_API_KEY` | ✅ | https://www.deepl.com/pro-api (Free tier 500,000 chars/월) |
| `STACK_EXCHANGE_KEY` | ⚪ 선택 | https://stackapps.com/apps/oauth/register (쿼터 10,000/일) |

`GITHUB_TOKEN`은 Actions가 자동 주입 → 별도 등록 불필요.

### 3. 수동 실행 테스트
Actions 탭 → `Daily Dev Trends` → `Run workflow` 버튼.
성공하면 Slack에 메시지 도착 + `reports/YYYY-MM-DD.md` 자동 커밋.

## 로컬 실행 (디버깅용)

```bash
pip install -r requirements.txt
export SLACK_WEBHOOK_URL='...'
export DEEPL_API_KEY='...'
export GH_API_TOKEN='ghp_...'        # fine-grained PAT, public_repo read
# export STACK_EXCHANGE_KEY='...'    # 선택
python fetch_trends.py
```

## 선정 로직

각 소스에서 상위 3개씩 후보 풀 구성 → 아래 공식으로 통합 점수 → 상위 5개 선정 (단, 활성 소스마다 최소 1개 보장):

```
score = log10(upvotes+1) * 1.0
      + log10(comments+1) * 1.5
      + log10(views+1) * 0.3   # views가 있는 경우만
```

조정 포인트는 `fetch_trends.py`의 `unified_score()`, `PER_SOURCE_POOL`, `TOP_N`.

## 번역 정책

1. 제목 기준 ASCII 알파벳 비율 > 60% → 영문으로 간주, 번역 실행
2. DeepL 우선 → 실패 시 `deep-translator`의 GoogleTranslator로 폴백
3. 둘 다 실패 시 원문 유지

### DeepL Free 쿼터 소진 후
현재 구성은 `deep-translator` (무공식 Google 스크레이퍼)로 자동 폴백합니다. 불안정할 수 있으므로 장기 운영 시:
- Papago API (Naver, 한국어 특화, 월 10,000자 무료)
- Google Cloud Translation API (유료, 월 500,000자 무료)
중 하나로 교체 권장. `_translate_google_fallback()`만 교체하면 됩니다.

## 소스별 주의사항

### Reddit
- 인증 없이 `hot.json` 사용. User-Agent 고정.
- Actions 공용 IP에서 429 발생 시 Reddit App 등록 후 OAuth `client_credentials` 전환 필요.

### Stack Overflow
- Stack Exchange API는 `sort=hot` 지원. 비인증 쿼터 300/일.
- `STACK_EXCHANGE_KEY` 등록 시 10,000/일로 확장.

### GitHub Discussions
- REST Search에는 discussion 타입이 없어 **GraphQL Search**를 사용.
- 필터: 최근 3일 내 업데이트 + 댓글 ≥20 + 최신순.
- 공개 Discussion만 대상. `GITHUB_TOKEN`으로 공개 데이터 조회 가능.
- 필터 조정: `fetch_github_discussions()`의 `q` 변수.

## cron 시간

`'0 23 * * *'` = UTC 23:00 = KST 08:00 (다음날).
GitHub Actions cron은 최대 15분 지연될 수 있음 (정시 보장 X).

## 확장 아이디어

- 제목뿐 아니라 본문 스니펫도 번역
- 개인 페이지(블로그/Notion)에 API 푸시
- 태그 기반 필터링 (예: Spring, Java, Kubernetes만)
- 중복 제거 (같은 URL이 여러 번 올라오는 경우 30일 LRU 캐시)
