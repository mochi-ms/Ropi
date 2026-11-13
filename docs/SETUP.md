# Ropi — 개발 환경 구축 가이드

> 구현 AI(Gemini Hydrogen Extra)는 **코딩 시작 전에 이 문서를 먼저 실행**한다.
> 대부분 `scripts/setup-dev.ps1` 한 번으로 끝난다.

## 1. 설치되는 도구

| 도구 | winget ID | 용도 | 비고 |
|---|---|---|---|
| Git | `Git.Git` | 버전 관리 | 관리자 권한(UAC) 거부 시 **MinGit 포터블**로 자동 대체 |
| Visual Studio Code | `Microsoft.VisualStudioCode` | 코드 편집기 | 사용자 범위 설치 |
| Python 3.12 (64-bit) | `Python.Python.3.12` | 런타임 | 3.13은 일부 AI 패키지 호환 문제로 사용 안 함 |
| FFmpeg | `Gyan.FFmpeg` | 오디오 변환 | YouTube 변환용 |
| GitHub CLI | `GitHub.cli` | GitHub 로그인/PR/릴리스 | `gh auth login` |
| (선택) NVIDIA 드라이버 + CUDA | — | 전사 가속 | PyTorch CUDA 휠이 런타임 포함, 드라이버만 최신이면 됨 |
| (Phase 6) Inno Setup | `JRSoftware.InnoSetup` | 설치 파일 제작 | 배포 단계에서만 |

**VS Code 확장**: Python, Pylance, Python Debugger, Ruff, Even Better TOML, GitLens, Qt for Python, Markdown All in One, Markdown Mermaid, GitHub Actions

## 2. 자동 설치 (권장)

PowerShell에서:

```powershell
# 저장소가 아직 없을 때: 스크립트만 내려받아 실행
irm https://raw.githubusercontent.com/mochi-ms/Ropi/main/scripts/setup-dev.ps1 -OutFile $env:TEMP\setup-dev.ps1
powershell -ExecutionPolicy Bypass -File $env:TEMP\setup-dev.ps1

# AI 전사 엔진(PyTorch, 약 2GB)까지 설치 — Phase 5부터 필요
powershell -ExecutionPolicy Bypass -File $env:USERPROFILE\dev\Ropi\scripts\setup-dev.ps1 -WithAI
```

스크립트가 하는 일:
1. winget으로 위 도구 설치 (이미 있으면 건너뜀)
2. Git 사용자 이름/이메일 설정 (없을 때만 물어봄)
3. `https://github.com/mochi-ms/Ropi.git` → `%USERPROFILE%\dev\Ropi` 로 clone
4. VS Code 확장 설치
5. `.venv` 생성 후 `requirements*.txt` 설치 (GPU 있으면 CUDA PyTorch, 없으면 CPU)

## 3. GitHub 인증 (최초 1회)

```powershell
gh auth login      # GitHub.com → HTTPS → 브라우저로 로그인
gh auth setup-git  # git push 시 자동 인증
```

## 4. 일상 개발 명령

```powershell
cd $env:USERPROFILE\dev\Ropi
.\.venv\Scripts\Activate.ps1

python -m ropi                 # 앱 실행
pytest -q                      # 테스트
pytest --cov=ropi              # 커버리지
ruff check . ; ruff format .   # 린트/포맷
pyright                        # 타입 체크
.\scripts\build.ps1            # exe 빌드 (Phase 6)
```

## 5. 계획된 requirements 파일 (Phase 0에서 작성)

`requirements.txt`
```
PySide6>=6.8,<7
mido>=1.3
pretty_midi>=0.2.10
pydantic>=2.7
yt-dlp>=2025.1.1
numpy>=1.26,<3
soundfile>=0.12
```

`requirements-dev.txt`
```
pytest>=8
pytest-qt>=4.4
pytest-cov>=5
ruff>=0.6
pyright>=1.1.380
pyinstaller>=6.10
```

`requirements-ai.txt` (torch는 스크립트가 GPU 여부에 따라 먼저 설치)
```
piano_transcription_inference>=0.0.5
basic-pitch[onnx]>=0.4
librosa>=0.10
```

> [!NOTE] 버전은 구현 시점에 `pip index versions <패키지>`로 확인하고, 동작 확인 후 `pip freeze > requirements.lock.txt`로 고정한다.

## 6. 문제 해결

| 증상 | 해결 |
|---|---|
| `winget` 없음 | Microsoft Store에서 "앱 설치 관리자(App Installer)" 업데이트 |
| 설치 후 `git`/`code` 명령 인식 안 됨 | PowerShell 창을 닫고 새로 열기 (PATH 갱신) |
| Git 설치 중 "관리자 권한" 실패 | 스크립트가 MinGit 포터블로 자동 대체함 |
| `Activate.ps1` 실행 정책 오류 | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` |
| `basic-pitch` 설치 실패 | 기본 엔진(ByteDance)만으로 진행, 이슈 기록 |
| Roblox에 키 입력이 안 들어감 | Ropi를 관리자 권한으로 실행하지 말 것(권한 수준 일치), 스캔코드 모드 확인 |
