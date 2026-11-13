# Ropi — 기술 결정 기록 (Architecture Decision Records)

| 날짜 | 결정 | 이유 |
|---|---|---|
| 2026-11-13 | Python 3.12 + PySide6 + SQLite + ctypes(Win32) 채택 | [SPEC.md](SPEC.md) 3.1 참고 |
| 2026-11-13 | 작업 루트를 `scratch/Ropi`로 유지 | 이미 Git 초기화 및 GitHub 원격 저장소(`origin/main`) 연결 완료 |
| 2026-11-13 | 상용 소프트웨어 수준의 모던 다크 UI/UX 원칙 고정 | 이모지·촌스러운 기본 아이콘 금지, 무료 오픈 라이선스 벡터 아이콘(Lucide Icons, ISC) + Pretendard 폰트 + Windows 11 다크 타이틀바·미세 테두리·세련된 컨트롤 적용 |
| 2026-11-13 | Roblox 비주얼 피아노 페달은 Hold(`Space` 누름 유지) 방식 채택 | 실제 게임 확인 결과 `Space`를 누르고 있는 동안만 페달이 활성화되므로 MIDI CC64 및 화음·프레이즈 기반 리페달링 시 최소 30ms 떼기 간격 보장 |
| 2026-11-13 | 스케줄러 대기는 `Event.wait` 대신 `time.sleep` + spin-wait 조합 사용 | Windows 11에서 백그라운드 프로세스 시 `Event.wait`는 ~15.6ms 틱으로 양자화되나 Python 3.12 `time.sleep`은 고해상도 대기 타이머(`~0.5ms`)를 사용하여 10k 이벤트 평균 오차 0.003ms 유지 |
