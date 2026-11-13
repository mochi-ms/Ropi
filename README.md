# 🎹 Ropi

Roblox 가상 피아노용 **자동 연주기 + 악보 라이브러리 + 인게임 오버레이(가사) + YouTube 피아노 음원 → MIDI 변환** 통합 Windows 프로그램.

> 현재 상태: **설계 완료 / 구현 준비 단계 (Phase 0)**

## 주요 기능 (계획)
- 🎼 **자동 연주** — MIDI(.mid) · VP 텍스트 악보 · `.ropi` 재생, 속도/조옮김/음역 자동 맞춤, 휴머나이즈, 연습 모드
- 📚 **라이브러리** — 폴더 트리, 태그, 즐겨찾기, 초성·가사 검색, 휴지통, 재생목록, 백업
- 🖥 **오버레이** — 게임 위 반투명 창, 클릭 통과, 곡 넘기기, 실시간 가사, 전역 핫키
- 🎤 **가사** — 탭 싱크 편집기, LRC 가져오기/내보내기
- 🔄 **YouTube → MIDI** — 링크만 넣으면 AI(ByteDance Piano Transcription / Spotify Basic Pitch)가 MIDI 생성 후 라이브러리에 자동 등록

## 문서
| 문서 | 내용 |
|---|---|
| [docs/SPEC.md](docs/SPEC.md) | 제품 사양서 (기능, 기술 스택, 아키텍처, DB, UI/UX) |
| [docs/ROADMAP.md](docs/ROADMAP.md) | Phase별 구현 계획 & 완료 기준 |
| [docs/SETUP.md](docs/SETUP.md) | 개발 환경 자동 설치 가이드 |
| [GEMINI.md](GEMINI.md) | 구현 AI 작업 규칙 |

## 빠른 시작 (개발자)
```powershell
irm https://raw.githubusercontent.com/mochi-ms/Ropi/main/scripts/setup-dev.ps1 -OutFile $env:TEMP\setup-dev.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\setup-dev.ps1
```

## 기술 스택
Python 3.12 · PySide6 (Qt 6) · SQLite (FTS5) · Win32 SendInput · yt-dlp · FFmpeg · PyTorch

## 주의
- YouTube 콘텐츠는 본인이 권리를 가진 것 또는 개인적 용도로만 변환하세요.
- 자동 입력 도구 사용에 따른 Roblox 계정 관련 책임은 사용자에게 있습니다.
