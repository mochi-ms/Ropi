# Ropi — 구현 진행 상황 (Progress)

## Phase 0 — 개발 환경 구축 & 프로젝트 골격
- [x] `scripts/setup-dev.ps1` 도구 설치 (Git, VS Code, Python 3.12, FFmpeg, GitHub CLI, VS Code 확장, `.venv`, 패키지)
- [x] `pyproject.toml` 및 `requirements*.txt` 작성
- [x] SPEC 6.3 폴더 구조 및 빈 패키지 생성
- [x] `.gitignore`, `.editorconfig`, `.vscode/*` 작성
- [x] `core/paths.py`, `core/logging_setup.py`, `core/errors.py`, `__main__.py` 구현
- [x] `.github/workflows/ci.yml` 작성
- [x] 완료 기준: `python -m ropi` 실행 시 빈 창 + 로그 파일 생성
- [x] 완료 기준: `pytest` / `ruff check` / `pyright` 통과
- [ ] 완료 기준: GitHub push 및 CI 통과

## Phase 1 — 코어: 음표 모델, 로더, 키맵, 재생 엔진
- [ ] `domain/note.py`, `domain/keymap.py`, `domain/arranger.py`
- [ ] `formats/midi_loader.py`, `formats/vp_sheet.py`
- [ ] `playback/win32_input.py`, `playback/timing.py`, `playback/focus_watcher.py`, `playback/scheduler.py`, `playback/service.py`
- [ ] `tools/play_cli.py`
- [ ] 완료 기준: 단위 테스트, 타이밍 오차 테스트(평균 ≤ 2ms, p99 ≤ 5ms), 안전장치 테스트
- [ ] 완료 기준: 실게임 검증 (사용자 확인)
- [ ] 완료 기준: domain/formats 커버리지 ≥ 85%

## Phase 2 — 라이브러리 백엔드
- [ ] `library/db.py`, `migrations/001_init.sql`, `file_store.py`, `repository.py`, `search.py`, `service.py`, `backup.py`
- [ ] `domain/hangul.py`, `formats/ropi_format.py`, `formats/lrc.py`
- [ ] 완료 기준: CRUD/휴지통/중복/마이그레이션/백업복원 테스트, 1만곡 검색 ≤ 100ms, 초성 검색, 커버리지 ≥ 85%

## Phase 3 — 메인 GUI
- [ ] 테마/폰트/아이콘, `MainWindow`, 라이브러리/지금재생/설정 페이지, 단일 인스턴스, 첫 실행 마법사
- [ ] 완료 기준: pytest-qt 시나리오, 1만곡 스크롤, DPI 확인, 실게임 검증

## Phase 4 — 오버레이, 가사, 전역 핫키
- [ ] `hotkeys/`, `ui/overlay/`, `domain/lyrics.py`, `pages/lyrics_editor_page.py`
- [ ] 완료 기준: 단위 테스트, 실게임 오버레이/클릭통과/핫키, 가사 싱크

## Phase 5 — YouTube / 오디오 → MIDI 변환
- [ ] `convert/` (yt-dlp, ffmpeg, worker, ByteDance/BasicPitch, postprocess, job_queue, service, UI)
- [ ] 완료 기준: mock + 10초 wav 통합 테스트, 워커 크래시 격리, 실사용 검증

## Phase 6 — 안정화, 선택 기능, 배포
- [ ] 장시간 테스트, 테마/번역, 선택 기능, PyInstaller/Inno Setup 빌드, 릴리스
