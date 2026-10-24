# PC를 꺼도 작동하는 무료 알리미 설정

감시 대상은 **롯데시네마 · 모든 영화 · 서울/경기/인천**입니다. GitHub의 공개 저장소와 표준 Ubuntu 실행기를 사용합니다. PC에 Python이나 별도 실행 프로그램을 설치할 필요가 없습니다. 현재 요금 기준 공개 저장소의 표준 실행기 사용은 무료입니다. 유료 실행기, 아티팩트, 캐시는 사용하지 않습니다.

## 1. 저장소 준비

GitHub에 **Public** 저장소를 만들고 이 프로젝트의 소스 파일과 `.github/workflows/notify.yml`을 올립니다. 기본 브랜치는 `main`으로 두면 됩니다. `data/`, `research/`, `.env` 및 개인 토큰 파일은 올리지 않습니다. 이 프로젝트에 포함된 `.gitignore`를 유지하세요.

무료 운영을 위해 이 워크플로는 비공개 저장소에서는 실행하지 않도록 설정되어 있습니다. GitHub 플러그인을 연결했다면 저장소 업로드 작업은 도와드릴 수 있습니다. 플러그인은 GitHub 작업을 돕는 연결이며, 감시는 GitHub Actions 서버가 실행합니다.

## 2. 텔레그램 봇과 대화 ID 준비

1. 텔레그램에서 공식 [@BotFather](https://t.me/BotFather)를 열고 `/newbot`을 보내 봇을 만듭니다.
2. 받은 봇 토큰을 보관합니다. 공개 코드, 이슈, 채팅에 붙여넣지 마세요.
3. **내가 만든 봇**의 대화창을 열고 `/start`를 보냅니다.
4. 이 PC의 `알리미 실행.cmd`를 열어 토큰을 넣고 **내 대화 찾기**를 누르면 대화 ID를 확인할 수 있습니다. GitHub 설정에는 이름을 제외한 숫자 ID를 사용합니다. PC 앱에서 감시를 시작할 필요는 없습니다.

## 3. GitHub Secrets 등록

저장소의 **Settings → Secrets and variables → Actions → Secrets → New repository secret**에서 두 개를 등록합니다.

| 이름 | 값 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | BotFather가 발급한 봇 토큰 |
| `TELEGRAM_CHAT_ID` | 알림을 받을 대화의 숫자 ID |

별도 GitHub 개인 토큰은 만들지 않습니다. 감시 기록 저장에는 GitHub가 자동으로 제공하는 실행 토큰을 사용합니다.

## 4. 테스트하고 감시 켜기

1. 저장소 **Actions → Lotte Stage Notifier → Run workflow**를 엽니다.
2. 모드를 **dry-run**으로 선택해 실행합니다. 롯데시네마 조회만 하며 메시지와 감시 기록을 만들지 않습니다. 실행 결과가 초록색인지 확인합니다.
3. 다시 **Run workflow**에서 **test**를 선택합니다. 텔레그램에 연결 확인 메시지가 도착하는지 확인합니다.
4. **Settings → Secrets and variables → Actions → Variables → New repository variable**에서 이름 **`MONITOR_ENABLED`**, 값 **`true`**를 등록합니다.
5. **Run workflow → check**를 한 번 실행합니다. 이후 5분 간격으로 자동 실행을 예약합니다. 첫 조회의 기존 예매 가능 회차는 기준으로 저장하고, 이후 새 회차나 대기/매진에서 예매 가능한 상태로 바뀐 미통보 회차를 알립니다.

중지할 때는 `MONITOR_ENABLED` 값을 `false`로 바꾸거나 Actions 메뉴에서 워크플로를 비활성화합니다. 수동 `check`는 변수와 관계없이 한 번 실행합니다.

## 알림 속도와 감시 범위

- 이벤트 공지에 포함된 수도권 무대인사를 먼저 확인한 뒤 실제 상영시간표에서 예매 가능 여부와 잔여석을 확인합니다. 공지 자체만으로 예매가 열렸다고 알리지 않습니다.
- 공지에 없는 회차도 찾기 위해 오늘부터 28일 시간표를 나누어 순회합니다. 한 실행에서 최대 180개 지점·날짜를 추가 조회하며, 약 220초의 조회 예산이 지나면 다음 실행으로 넘깁니다. 전체 순회에는 정상 실행 기준 대략 1시간 이상 걸릴 수 있고 GitHub 지연이나 서버 상태에 따라 더 길어집니다. 공지에 있는 회차에는 28일 제한을 적용하지 않습니다.
- 처음 확인한 지점·날짜의 기존 예매 가능 회차는 일괄 전송하지 않습니다. 첫 공지 점검 이후 새 공지에 등장한 지점·날짜의 회차는 알림 대상입니다.
- 한 번 성공적으로 알린 회차는 다시 알리지 않습니다. 취소표를 반복 통보하는 기능은 아닙니다. 조회 사이에 잠깐 열렸다 닫히거나 사이트가 무대인사 표시를 제공하지 않은 회차는 놓칠 수 있습니다.
- GitHub 예약은 정확한 시각을 보장하지 않습니다. 실행이 늦거나 생략될 수 있고, 공개 저장소에 60일간 활동이 없으면 예약 작업이 비활성화될 수 있습니다. 가끔 Actions의 최근 성공 시간을 확인하고, 비활성화되었다면 다시 켜 주세요.

## 알림 기록과 오류 확인

`notifier-state` 브랜치의 `notifier-state.json`에 회차의 해시, 날짜, 전송 상태, 순회 위치를 저장합니다. 봇 토큰·대화 ID·개인 이름은 기록하지 않습니다. `main`에는 소스 코드만 유지합니다. 이 브랜치와 파일을 수동 삭제하면 감시 기준이 달라질 수 있으므로 유지하세요.

알림 전후로 기록을 저장하며 전송에 실패한 회차는 다음 조회에서 재시도합니다. 텔레그램이 메시지를 받았지만 응답이 유실되거나 전송 직후 저장이 실패하면 드물게 중복될 수 있습니다. 오류가 발생하면 **Actions → 해당 실행 → Check and notify** 로그를 확인하세요.

| 증상 | 확인할 내용 |
|---|---|
| 실행이 Skipped로 표시됨 | 공개 저장소인지, `MONITOR_ENABLED=true`인지 확인 |
| 텔레그램 설정 오류 | Secrets 두 개의 이름/값, 내 봇에 `/start` 전송 여부 확인 |
| GitHub 기록 저장 403 | Settings → Actions → General의 워크플로 권한 및 저장소 정책이 쓰기를 허용하는지 확인 |
| 롯데시네마 조회 실패 | 다음 실행 결과 확인. GitHub 실행 지역에서의 접속 제한이나 사이트 변경은 코드 수정이 필요할 수 있음 |
| 실행 성공인데 알림 없음 | 새 예매 가능 회차가 없으면 메시지를 보내지 않음. 첫 실행 기존 회차도 기준 저장만 수행 |

## 개발 검증

```sh
python -m unittest discover -s tests -v
python -m notifier.cloud --mode dry-run --sweep-count 2 --max-seconds 90
```

Python 3.12 표준 라이브러리만 사용합니다. Windows GUI와 별개로 Linux에서 실행하도록 만들었습니다. 실제 GitHub 실행기에서의 접속과 텔레그램 수신은 업로드 후 위 테스트로 확인해야 합니다.

공식 근거: [GitHub Actions 요금](https://docs.github.com/en/billing/concepts/product-billing/github-actions), [예약 실행과 제한](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule), [Secrets 등록](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-secrets), [Telegram 봇 만들기](https://core.telegram.org/bots/tutorial).
