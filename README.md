# Teach Me

매일 뉴스 피드에서 기사를 수집하고, 실행 시 지정한 학습 언어와 CEFR 수준에 맞는 짧은 일일 읽기 자료를 생성하는 CLI 도구입니다. 여러 기사를 하나의 다이제스트로 요약해 날짜 폴더의 `<언어>.md` 파일로 저장합니다.

## 요구 사항

- Linux 또는 POSIX 호환 셸
- Python 3.10 이상
- OpenAI 호환 Chat Completions API 계정과 사용할 수 있는 모델
- 공개 RSS/Atom 피드 또는 기사를 공개한 뉴스 목록 URL

런타임은 Python 표준 라이브러리만 사용하므로 별도 pip 패키지 설치가 필요 없습니다.

## 설치

저장소를 받은 뒤 프로젝트 디렉터리에서 실행합니다.

```sh
git clone <repository-url> teachme
cd teachme
./install.sh
```

설치 스크립트는 Python 버전을 확인하고 `run.sh` 실행 권한, `.env`, `result/`, `logs/`를 준비합니다. 기존 `.env`가 있으면 덮어쓰지 않습니다. 수동 설치는 다음과 같습니다.

```sh
cp .env.example .env
chmod 600 .env
chmod +x run.sh
mkdir -p result logs
```

## 설정

`.env`를 열어 LLM 제공자 설정을 입력합니다.

```dotenv
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=your-api-key
LLM_MODEL=your-model-id

TIMEZONE=Europe/Brussels
```

`LLM_BASE_URL`은 `/v1`까지 포함하는 OpenAI 호환 API 기본 URL입니다. `LLM_API_KEY`와 `LLM_MODEL`은 필수이며 나머지 설정은 `.env.example`에 기본값이 있습니다. 학습 언어와 CEFR 수준은 `.env`에서 설정하지 않고 매 실행의 `--language`, `--level` 옵션으로 지정합니다. 기존 `.env`의 `LEARNING_LANGUAGE`와 `CEFR_LEVEL`은 사용하지 않습니다. API 키를 README, 로그, 버전 관리에 올리지 마세요. `.env`는 `.gitignore`에 포함되어 있습니다.

기본 뉴스 출처는 BBC, The Guardian, CNN, Associated Press, Le Monde, The Brussels Times입니다. `NEWS_SOURCES_JSON`을 지정하면 기본 출처 전체를 대체합니다.

```dotenv
NEWS_SOURCES_JSON='[
    {"id": "bbc", "name": "BBC News", "url": "https://feeds.bbci.co.uk/news/rss.xml"},
    {"id": "example", "name": "Example News", "url": "https://example.com/feed.xml"}
]'
```

배열은 위처럼 작은따옴표로 감싼 여러 줄 형식 또는 따옴표 없는 여러 줄 JSON으로 작성할 수 있습니다.

각 URL은 RSS/Atom 피드 주소 또는 공개 뉴스 목록 페이지일 수 있습니다. RSS/Atom을 우선 사용하고, 공개 피드가 없으면 목록 페이지에서 같은 사이트의 주요 기사 제목·링크를 고른 뒤 공개 기사 본문을 읽습니다. 기사당 본문 발췌는 입력 길이 제한을 적용하며 로그인·유료벽·접근 통제를 우회하지 않습니다. 해당 페이지가 주요 기사 링크나 읽을 수 있는 본문을 제공하지 않으면 출처 상태에 수집 실패가 기록됩니다.

주요 설정:

| 변수 | 기본값 | 설명 |
|---|---|---|
| `MAX_ARTICLES_PER_SOURCE` | `3` | 출처별 입력 후보 수 상한 |
| `MAX_TOTAL_INPUT_CHARS` | `24000` | LLM에 전달할 전체 뉴스 입력 크기 상한 |
| `TIMEZONE` | `UTC` | 실행 날짜와 결과 디렉터리에 사용할 IANA 시간대 |
| `HTTP_TIMEOUT_SECONDS` | `20` | 피드 및 API 요청 타임아웃 |
| `HTTP_MAX_RETRIES` | `3` | 네트워크·429·일시적 서버 오류 재시도 횟수 |
| `LLM_MAX_TOKENS` | `4000` | 모델 출력 최대 토큰 수. 추론 모델은 사고 과정에도 토큰을 쓰므로 넉넉히 두고, 제공자의 분당 출력 한도가 낮으면 줄입니다. |
| `OUTPUT_DIR` | `result` | 생성 결과 디렉터리 |
| `PROMPT_FILE` | `prompts/news_to_lesson.md` | 파일 기반 생성 프롬프트 |

## 사용법

```sh
./run.sh --language English --level B1
```

`--language`와 `--level`은 필수입니다. CEFR 수준은 A1-C2에서 선택합니다. 문제 조사에는 `--debug`, 특정 날짜 지정에는 `--date`, 같은 날짜·언어 자료를 다시 생성할 때는 `--force`를 사용합니다.

```sh
./run.sh --language English --level B1 --debug
./run.sh --language French --level A2 --date 2026-10-05
./run.sh --language French --level A2 --date 2026-10-05 --force
```

`--force` 실행 중 생성에 실패하면 해당 언어의 기존 결과 파일을 보존합니다. 디버그 로그에도 API 키는 출력하지 않습니다.

## 결과

```text
result/
└── YYYY-MM-DD/
    ├── English.md
    ├── French.md
    └── run-summary.json
```

`<언어>.md`는 해당 실행에서 선택한 언어로 만든 일일 학습자료입니다. Reading은 250-350단어를 목표로 하고 어휘·질문·답안을 포함해 약 3-5분 분량입니다. 요약에는 기사별 출처 링크와 학습 언어·수준이 포함됩니다. 서로 다른 언어의 결과는 같은 날짜 폴더에 함께 보관되며 `run-summary.json`은 가장 최근 실행 요약입니다.

같은 날짜·언어의 기존 자료는 기본적으로 건너뜁니다. 처리 결과가 없으면 학습자료 파일 없이 실행 요약만 생성됩니다. 종료 코드는 정상 `0`, 부분 실패 `1`, 설정·실행 치명 오류 `2`, 사용자 인터럽트 `130`입니다.

## 메일 전송

`.env`에 `NOTIFY_EMAIL`을 지정하면 학습자료를 생성하고 저장한 직후 [Resend](https://resend.com)로 해당 주소에 메일을 보냅니다. 학습자료 전체가 메일 **본문**에 제목·목록·링크가 보이는 HTML로 렌더링되어 첨부 없이 바로 읽을 수 있고, HTML을 표시하지 못하는 메일 클라이언트용으로 마크다운 텍스트도 함께 담깁니다. 기사·모델이 만든 텍스트는 모두 이스케이프되어 본문에 태그나 스크립트가 들어가지 않습니다. `NOTIFY_EMAIL`이 없으면 메일을 보내지 않습니다.

```dotenv
NOTIFY_EMAIL=reader@example.com,other@example.com
RESEND_API_KEY=re_xxxxxxxx
MAIL_FROM=teachme@your-verified-domain.com
```

- `RESEND_API_KEY`는 Resend 대시보드의 API Keys에서 발급합니다. 수신자는 쉼표로 최대 50명까지 지정할 수 있습니다.
- `MAIL_FROM`을 생략하면 Resend 샌드박스 주소 `onboarding@resend.dev`를 사용합니다. 이 주소는 **Resend 계정 가입 메일로만** 보낼 수 있으므로, 다른 주소로 보내려면 Resend에서 도메인을 인증하고 그 도메인의 주소를 `MAIL_FROM`에 지정하세요.
- 설정 오류(잘못된 주소, `RESEND_API_KEY` 누락 등)는 뉴스를 수집하기 전에 종료 코드 `2`로 알려 줍니다.
- 전송에 실패해도 결과 파일은 보존되고 종료 코드 `1`을 반환하며 `run-summary.json`의 `email`과 `errors`에 기록됩니다. Resend가 돌려준 오류 사유는 표시하고 API 키는 로그와 요약에 남지 않습니다.
- 같은 날짜·언어 자료가 이미 있어 건너뛴 실행은 메일을 다시 보내지 않습니다. 같은 날 다시 보내려면 `--force`로 재생성하세요.

메일 설정만 확인하려면 뉴스 수집과 LLM 호출 없이 샘플 메일을 한 통 보내 보세요. `--language`는 필요 없고 결과 파일은 만들지 않습니다.

```sh
./run.sh --test-email
```

성공하면 종료 코드 `0`, `NOTIFY_EMAIL`/`RESEND_API_KEY` 누락은 `2`, 전송 실패는 `1`입니다. `LLM_*` 설정은 다른 실행과 같이 필요합니다.

## Cron

매일 06:30에 실행하는 예시입니다. 실제 경로와 cron 실행 계정에 맞게 수정하세요.

```cron
30 6 * * * /absolute/path/to/teachme/run.sh --language English --level B1 >> /absolute/path/to/teachme/logs/cron.log 2>&1
```

cron 실행 계정이 `.env`를 읽고 결과 및 로그 디렉터리에 쓸 수 있어야 합니다. `TIMEZONE`과 서버의 cron 시간대도 함께 확인하세요.

## 테스트

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 문제 해결

- `LLM authentication failed (HTTP 401)`: API 키가 올바른 제공자에서 발급됐는지, 만료·폐기되지 않았는지 확인합니다.
- `LLM access forbidden (HTTP 403)`: API 키의 프로젝트/조직 권한, 계정 상태, 모델 사용 권한을 확인합니다. `.env`의 `LLM_BASE_URL`과 `LLM_MODEL`이 같은 제공자의 호환 API 및 활성 모델을 가리키는지도 확인하세요. debug 로그에는 제공자 오류 코드가 있으면 이를, 오류 본문이 비어 있으면 응답 형식과 request ID를 표시합니다. request ID로 제공자 지원팀에 문의할 수 있습니다. 키 값은 표시하지 않습니다.
- `Email delivery failed (HTTP 403: ...)`: 기본 발신 주소 `onboarding@resend.dev`는 Resend 계정 소유자의 메일로만 보낼 수 있습니다. Resend에서 도메인을 인증하고 `MAIL_FROM`을 그 도메인의 주소로 지정하세요. `HTTP 401`이면 `RESEND_API_KEY`를 확인합니다.
- `LLM request failed (HTTP 404)`: API 기본 URL의 `/v1` 경로와 Chat Completions 지원 여부를 확인합니다.
- `LLM returned an empty lesson (finish_reason=length ...)`: 모델이 출력 상한을 사고 과정에 모두 써서 본문이 비었습니다. `LLM_MAX_TOKENS`를 늘리거나 추론 기능이 없는 모델을 사용하세요.
- `LLM output token limit exceeded (HTTP 429)`: 제공자 계정의 OTPM 한도보다 요청 출력 토큰이 큽니다. `.env`에서 `LLM_MAX_TOKENS`를 더 낮추세요. 이 한도 오류는 재시도해도 해결되지 않아 한 번만 요청합니다.

## 문서

상세 요구사항, 설계, 설정 계약, 테스트 및 운영 절차는 [개발 문서](doc/README.md)를 참고하세요.