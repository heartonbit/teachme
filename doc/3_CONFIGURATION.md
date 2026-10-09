# 설정 및 인터페이스 규격

## 1. 환경 설정

실행은 저장소 루트의 `.env`를 읽는다. `.env.example`에는 설명과 비밀값이 아닌 기본값만 둔다. `.env`는 버전 관리에 포함하지 않는다.

| 변수 | 필수 | 기본값/예시 | 설명 |
|---|---:|---|---|
| `LLM_BASE_URL` | 예 | 제공자 OpenAI 호환 API 기본 URL | `/v1` 포함 여부와 최종 경로 조합 규칙을 구현에서 일관되게 정의한다. |
| `LLM_API_KEY` | 예 | 없음 | API 인증 비밀값. 로그에 출력 금지. |
| `LLM_MODEL` | 예 | 제공자 지정 모델 ID | 사용할 모델 식별자. |
| `MAX_ARTICLES_PER_SOURCE` | 아니오 | `3` | 일일 통합 요약 입력을 위한 미디어별 최대 후보 수, 1 이상의 정수. |
| `MAX_TOTAL_INPUT_CHARS` | 아니오 | `24000` | LLM에 전달하는 전체 뉴스 입력의 최대 문자 수. 초과 시 본문 발췌를 줄이고 출처 메타데이터는 유지한다. |
| `NEWS_SOURCES_JSON` | 권장 | 아래 JSON | 미디어 ID, 표시명, 공개 시작 URL 목록. 설정 시 기본 목록을 대체한다. |
| `TIMEZONE` | 아니오 | `UTC` | 실행 날짜 및 날짜 폴더의 IANA 시간대. |
| `HTTP_TIMEOUT_SECONDS` | 아니오 | `20` | 외부 HTTP 요청 타임아웃. |
| `HTTP_MAX_RETRIES` | 아니오 | `3` | 일시적 오류의 최대 재시도 수. 최초 요청은 재시도 수에 포함하지 않는다. |
| `LLM_MAX_TOKENS` | 아니오 | `4000` | 생성 응답 상한. 추론 모델은 사고 토큰을 포함하므로 넉넉히 두고, 제공자 OTPM 제한이 낮으면 줄인다. 제공자가 지원하는 경우에만 전달한다. |
| `OUTPUT_DIR` | 아니오 | `result` | 산출물 루트. |
| `PROMPT_FILE` | 아니오 | `prompts/news_to_lesson.md` | 프롬프트 템플릿 경로. |
| `LOG_LEVEL` | 아니오 | `INFO` | 기본 로그 수준. `--debug`는 `DEBUG`로 올린다. |
| `NOTIFY_EMAIL` | 아니오 | 없음 | 쉼표·세미콜론·공백으로 구분한 수신 주소(최대 50명). 설정하면 생성 성공 후 Resend로 학습자료를 전송하고, 비우면 전송하지 않는다. |
| `RESEND_API_KEY` | `NOTIFY_EMAIL` 사용 시 | 없음 | Resend API 키. 로그와 요약에 기록하지 않는다. |
| `MAIL_FROM` | 아니오 | `onboarding@resend.dev` | 발신 주소(순수 이메일 주소). 기본 샌드박스 주소는 Resend 계정 소유자의 메일로만 보낼 수 있어, 다른 수신자에게 보내려면 인증한 도메인의 주소를 지정한다. |

기본 `NEWS_SOURCES_JSON` 제안:

```json
[
  {"id":"bbc","name":"BBC","url":"https://www.bbc.com/news"},
  {"id":"guardian","name":"The Guardian","url":"https://www.theguardian.com/international"},
  {"id":"cnn","name":"CNN","url":"https://www.cnn.com"},
  {"id":"ap","name":"Associated Press","url":"https://apnews.com"},
  {"id":"lemonde","name":"Le Monde","url":"https://www.lemonde.fr"},
  {"id":"brusselstimes","name":"The Brussels Times","url":"https://www.brusselstimes.com/"}
]
```

`NEWS_SOURCES_JSON`는 `.env`에서 작은따옴표로 감싼 여러 줄 값 또는 따옴표 없는 여러 줄 JSON 배열로 저장할 수 있다. 줄 연속은 따옴표 및 JSON 괄호 균형으로 판별한다. 파서는 JSON 문법, 고유 `id`, 유효한 `http`/`https` URL, 필수 키를 검증한다. 자격 증명 또는 `token`, `secret`, `password`, `api_key`, `auth` 계열 쿼리 변수가 있는 URL은 거부한다. RSS/API 주소가 확인되면 해당 공개 피드 주소를 `url`로 지정하거나 수집기 설정에 별도 피드 URL을 추가한다. 기본 홈페이지 URL만으로 모든 사이트의 최신 기사를 안정적으로 수집할 수 있다고 가정하지 않는다.

## 2. CLI 계약

진입점은 `./run.sh`로 한다. 아래 옵션명은 제안 계약이며 구현에서 동일하게 유지한다.

```text
./run.sh (--language LANGUAGE --level LEVEL | --test-email) [--debug] [--date YYYY-MM-DD] [--force]
```

- `--language LANGUAGE`: `--test-email`이 없으면 필수. 이 실행의 자료 언어를 지정하며 `.env`의 `LEARNING_LANGUAGE`는 사용하지 않는다.
- `--level LEVEL`: `--test-email`이 없으면 필수. 해당 실행 언어의 CEFR 수준을 `A1`, `A2`, `B1`, `B2`, `C1`, `C2` 중에서 지정한다. 기존 `.env`의 `CEFR_LEVEL`은 사용하지 않는다.
- `--test-email`: 수집·LLM 호출·결과 파일 생성 없이 설정된 Resend로 샘플 메일 한 통만 보내고 종료한다. 종료 코드는 성공 `0`, 메일 설정 누락 `2`, 전송 실패 `1`.
- `--debug`: 상세 진단 로그 및 traceback 활성화. 비밀값은 계속 마스킹한다.
- `--date YYYY-MM-DD`: 지정한 날짜 폴더에 기록한다. 생략하면 설정한 시간대의 오늘 날짜를 사용한다.
- `--force`: 같은 날짜의 기존 파일을 재생성한다. 사용 전 로그에 덮어쓰기 동작을 표시한다.
- 잘못된 인자나 날짜는 네트워크 요청 전에 사용법과 함께 거부한다.

종료 코드:

| 코드 | 의미 |
|---:|---|
| `0` | 정상 완료. 처리할 뉴스가 없어도 정상이다. |
| `1` | 일부 뉴스/출처 실패가 있는 부분 성공. 실행 요약은 기록한다. |
| `2` | 설정, 실행 환경, 출력 경로 등 실행을 시작할 수 없는 치명 오류. |
| `130` | 사용자가 인터럽트한 실행. |

## 3. 산출물 계약

```text
result/
└── YYYY-MM-DD/
    ├── run-summary.json
    ├── English.md                  # --language English
    └── French.md                   # --language French
```

  날짜는 `TIMEZONE` 기준이며 ISO 8601 `YYYY-MM-DD` 형식이다. 실행마다 전체 수집분을 요약한 `<언어>.md` 한 개를 생성하며 같은 날짜 폴더에 언어별 파일을 함께 보관할 수 있다. 파일명에 사용할 언어 값은 경로 구분자 등 위험 문자를 정규화한다. 재실행 기본 동작은 같은 날짜·언어의 기존 자료를 건너뛰는 것이며, `--force`는 해당 언어 파일을 재생성한다.

학습자료 Markdown 필수 메타데이터 및 섹션:

```markdown
# {당일 뉴스 다이제스트 제목}

- Generated: {ISO 8601 시각}
- Language / Level: {언어} / {CEFR 수준}
- Model: {실제 응답 모델} (요청 모델과 다르면 `(requested: {요청 모델})` 병기)
- Estimated study time: 3-5 minutes

## Reading
{당일 수집된 여러 뉴스의 핵심을 종합한 250-350단어의 짧은 텍스트}

## Vocabulary
{핵심 어휘 5개}

## Comprehension
{이해 질문 3개}

## Answer Key
{간결한 답}

## Sources
- [{매체명}: {기사 제목}]({기사 URL})
- ...
```

`<언어>.md`의 Reading은 250-350단어를 목표로 하고 어휘·질문·답안을 포함한 전체 자료는 약 3-5분 안에 학습할 수 있어야 한다. 다이제스트의 핵심 내용과 출처 링크를 연결해 표시한다. `Sources` 목록은 누락을 막기 위해 모델 응답이 아닌 수집 메타데이터에서 애플리케이션이 렌더링한다. 실행 요약 JSON은 최소한 `run_id`, `started_at`, `finished_at`, `date`, `timezone`, `language`, `level`, `model`, `prompt_file`, `prompt_version`, `counts`(discovered/deduplicated/generated/skipped/failed; `generated`는 0 또는 1), `sources`(각 ID와 상태·수집 건수·안전한 오류 코드)를 기록한다. `deduplicated`는 중복 제거 후 남은 고유 뉴스 수다. API 키와 기사 전문은 기록하지 않는다.

현재 수집기는 RSS/Atom 피드 URL을 직접 지정하거나 설정된 공개 페이지에서 `<link rel="alternate">` 피드 링크를 찾는다. 일반 HTML 기사 목록을 직접 파싱하는 사이트별 수집기는 아직 구현되지 않았다. 공개 피드를 제공하지 않는 출처는 실행 요약에 수집 실패로 남으며, 해당 사이트의 피드 URL을 `NEWS_SOURCES_JSON`에 지정해야 한다.

## 4. 로그 형식

- 로그는 stdout 또는 stderr 중 한 경로로 일관되게 출력하고, cron에서는 별도 로그 파일로 리디렉션한다.
- 기본 로그는 실행 시작/종료, 출처별 건수, 실패 코드만 포함한다.
- `--debug`는 요청 단계, 재시도 횟수, 파싱 원인을 추가한다. Authorization 헤더, `.env` 전체, 비밀 쿼리 파라미터는 절대 출력하지 않는다.