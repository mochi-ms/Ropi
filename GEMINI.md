# GEMINI.md — 구현 AI 작업 지침 (Gemini Hydrogen Extra 필독)

이 저장소는 **Ropi**(Roblox 가상 피아노 자동 연주 + 라이브러리 + 오버레이 + YouTube→MIDI 변환) 프로젝트다.

## 반드시 먼저 읽을 문서 (순서대로)
1. [docs/SPEC.md](docs/SPEC.md) — 무엇을 만드는가 (요구사항 ID, 아키텍처, DB, UI/UX)
2. [docs/ROADMAP.md](docs/ROADMAP.md) — 어떤 순서로 만드는가 (Phase별 작업 & 완료 기준)
3. [docs/SETUP.md](docs/SETUP.md) — 개발 환경 (`scripts/setup-dev.ps1` 먼저 실행)

## 작업 규칙
1. **Phase 순서를 지킨다.** 현재 Phase의 완료 기준을 모두 체크하기 전에는 다음 Phase로 가지 않는다. 진행 상황은 `docs/PROGRESS.md`에 체크박스로 기록한다.
2. **스택 고정**: Python 3.12, PySide6, SQLite, ctypes(Win32). SPEC 3.2에 없는 의존성을 추가할 때는 `docs/DECISIONS.md`에 이유를 남긴다.
3. **계층 규칙**: `domain/`, `formats/`는 Qt·Win32를 import하지 않는다. UI는 서비스만 호출하고, 서비스는 UI를 모른다.
4. **UI 스레드 블로킹 금지**: 파일/DB 대량 작업·네트워크·전사는 워커 스레드/프로세스로.
5. **키 안전장치**: 키 입력 코드는 반드시 `try/finally`로 `release_all()`을 보장한다. 이 규칙을 깨는 코드는 머지 금지.
6. **예외 처리**: 사용자 입력/파일/네트워크 경계에서는 구체적 예외를 잡아 한국어 메시지로 변환. 내부 로직에서 `except Exception: pass` 금지.
7. **타입 힌트 100%**, `pyright` basic 이상 통과, `ruff` 통과. 함수는 50줄 이내 권장.
8. **테스트 먼저/함께**: 새 기능 = 새 테스트. 실제 Win32 입력 대신 `FakeInputBackend` 사용.
9. **라이선스**: GPL 오픈소스(예: 일부 Roblox 오토플레이어)는 코드 복사 금지, 아이디어만 참고. 새 의존성 라이선스는 `THIRD_PARTY_LICENSES.md`에 기록.
10. **UI 문자열**은 모두 `self.tr()`로 감싸고 기본 언어는 한국어.
11. **커밋**: Conventional Commits (`feat:`, `fix:`, `test:`, `docs:`, `refactor:`, `chore:`), 작은 단위로 자주. Phase 완료 시 `git tag phase-N` 후 push.
12. 실게임 검증이 필요한 항목(ROADMAP "실게임 검증")은 사용자에게 테스트를 요청하고 결과를 `docs/PROGRESS.md`에 기록한다.

## 자주 쓰는 명령
```powershell
.\.venv\Scripts\Activate.ps1
python -m ropi
pytest -q ; ruff check . ; pyright
```
