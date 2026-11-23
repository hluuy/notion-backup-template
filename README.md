# Notion 강의 필기 자동 백업

노션 강의 필기를 매일 03:00(한국 시간)에 Markdown으로 바꿔 이 **비공개** 저장소의 `notes/` 폴더에 쌓습니다. GitHub Actions에서 돌아가므로 내 컴퓨터가 꺼져 있어도 동작하고, 노션은 읽기만 합니다.

## 처음 설정 (약 10분)

1. **저장소 만들기**: 이 템플릿에서 Use this template → Create a new repository. 공개 범위는 반드시 **Private**. Public이면 백업이 실행되지 않고, 나중에 Public으로 바꾸면 그때까지의 필기가 모두 공개됩니다.
2. **노션 통합 만들기**: <https://www.notion.so/developers> (노션 개발자 포털) → 새 통합 → 유형 내부, 이름 `backup`. 권한은 아래 표대로 설정하고 내부 통합 시크릿을 복사합니다.
3. **페이지 연결하기**: 백업할 최상위 페이지마다 오른쪽 위 `···` → 연결 → `backup`. 하위 페이지는 자동으로 포함됩니다.
4. **토큰 등록하기**: 저장소 Settings → Secrets and variables → Actions → New repository secret. 이름 `NOTION_TOKEN`, 값은 2단계의 시크릿. 등록하기 전의 예약 실행은 실패 없이 건너뜁니다.
5. **Actions 켜기**: Actions 탭에서 워크플로 사용을 허용합니다.
6. **첫 백업**: Actions → Notion Backup → Run workflow.
7. **확인**: 끝나면 `notes/`에 필기가 생깁니다. 필기가 많으면 첫 백업이 며칠(하루 한 번 실행)에 걸쳐 끝납니다.

| 노션 통합 권한 | 설정 |
| --- | --- |
| 콘텐츠 읽기 | 켬 |
| 콘텐츠 업데이트 / 삽입 | 끔 |
| 댓글 읽기 / 삽입 | 끔 |
| 사용자 정보 | 이메일 주소 없이 읽기 |

## 자주 하는 일

| 하고 싶은 일 | 방법 |
| --- | --- |
| 백업할 페이지 늘리기 | 노션에서 `backup` 통합에 연결. 다음 실행 후에도 안 나오면 `config.yml`의 `roots`에 링크 추가 |
| 일부 빼기 | `config.yml`의 `exclude`에 링크 추가. 많이 빼면 `allow_mass_delete`를 켜고 수동 실행 |
| 지금 바로 백업 | Actions → Notion Backup → Run workflow (모바일 앱도 가능) |
| "확인 필요" 정리 | 정말 지운 페이지면 Summary의 ID를 `exclude`에 넣기. 삭제된 뒤 그 줄은 지워도 됨 |
| 예전 필기 찾기 | 파일의 History. 삭제된 파일은 폴더의 커밋 기록 |
| 변환 규칙 바뀐 뒤 전부 다시 | Run workflow에서 `full_rebuild` 켜기 |
| 컴퓨터에서 보기 | clone 후 `notes/`를 Obsidian vault로 열기. Windows는 먼저 `git config --global core.longpaths true`, clone 위치는 `C:\notes`처럼 짧게 |

옵션을 켠 수동 실행은 03:00 무렵을 피하세요. 대기 중에 예약 실행이 들어오면 취소(Cancelled)됩니다.

## 실행 결과

| 결과 | 뜻 | 커밋 | 워크플로 |
| --- | --- | --- | --- |
| `ok` | 모두 성공했거나 경고만 있음 | 함 | 성공 |
| `partial` | 일부 페이지나 범위가 실패 | 성공분만 | 실패 표시 |
| `abort` | 안전 검사·설정 오류로 중단 | 안 함 | 실패 |

자세한 내용은 실행 화면의 Summary에 나옵니다.

## 저장소 구조

```
backup.py                   실행 시작점
notion_backup/
  client.py                 노션 API 호출, 재시도, 요청 간격
  tree.py                   탐색, 최상위 정하기, 순환 재탐색
  pages.py                  페이지 내용 받기, 렌더링, 이미지 준비
  convert.py                블록을 Markdown으로
  properties.py             데이터베이스 속성, front matter, 시각
  paths.py, naming.py       저장 경로와 파일 이름
  sync.py                   경로 변경 묶음, 반영과 되돌리기
  lists.py                  목차, 데이터베이스 README, CSV
  state.py                  상태 파일과 상태 복구
  runner.py                 한 번의 실행 전체 흐름
config.yml                  제외 목록 등
tests/                      테스트(가짜 노션으로 동작 검증)
notes/                      백업 결과물
```

## 개발

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q

# 실제 노션으로 계획만 보기(파일을 바꾸지 않음)
NOTION_TOKEN=... python backup.py --dry-run
# 한 페이지 변환 결과 보기
NOTION_TOKEN=... python backup.py --only <페이지 링크>
```

로컬 실행 결과가 `abort`면 `notes/`가 일부만 반영되었을 수 있습니다. `git checkout -- notes/`와 `git clean -fd notes/`로 되돌리세요.
