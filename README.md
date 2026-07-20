# CRACK 🛣️⚡

> 모두가 맘편히 이용할 수 있는 **안전한 도로**를 만듭니다.

CRACK은 시민이 스마트폰으로 촬영한 사진/영상을 **YOLOv8 기반 AI**가 자동으로 판독하여 포트홀(pothole)·싱크홀(sinkhole) 등 도로 파손을 탐지하고, 이를 지도 기반으로 신고·공유·관리할 수 있게 해주는 **Smart Road Safety Platform**입니다.

> 이 저장소는 5인 팀 프로젝트 CRACK을 정리한 포트폴리오 저장소입니다. 팀원들과 함께 만든 코드라서, 제가 직접 담당한 부분만 아래 [개인 기여 범위](#개인-기여-범위)에 따로 적어두었습니다.

---

## 목차

- [프로젝트 목표](#프로젝트-목표)
- [주요 기능](#주요-기능)
- [기술 스택](#기술-스택)
- [AI 모델](#ai-모델)
- [프로젝트 구조](#프로젝트-구조)
- [시작하기](#시작하기)
- [데이터베이스 모델](#데이터베이스-모델)
- [팀 소개](#팀-소개)
- [개인 기여 범위](#개인-기여-범위)
- [라이선스](#라이선스)

---

## 프로젝트 목표

1. **AI 도로파손 자동탐지** — YOLOv8s 비전 모델로 사진/영상을 판독해 포트홀·싱크홀 여부를 자동 검증합니다. MVP에서는 AI 탐지 결과와 설정된 임계값을 기준으로 신고를 자동 분류하며, 기준을 충족하지 못한 신고는 관리자 확인 전에 반려 상태로 처리합니다.
2. **시민참여 위험공유** — 신고 → 실시간 Alert 피드 → 관심지역/거리 기반 알림으로 이어지는 흐름을 통해 도로 위험 정보를 빠르게 공유합니다.
3. **안전한 운영체계 구축** — GPS 기반 중복/허위 제보 차단, 신고 제출 시 이미지 재인코딩을 통한 EXIF 제거, 관리자 대시보드를 통한 통계·회원·신고 관리로 지속 가능한 서비스를 지향합니다. 실제 운영 환경에서는 오탐·미탐에 대비한 관리자 재검토 절차와 이의제기 기능 보완이 추가로 필요합니다.

## 주요 기능

### 시민(사용자) 기능
- **AI 도로파손 제보**: 이미지(jpg/png/gif/heic/heif) 및 영상(mp4/mov/avi/m4v) 업로드
  - EXIF/영상 메타데이터에서 GPS 좌표 자동 추출 (piexif → exifread → Pillow 3단계 폴백, 영상은 바이너리 파싱 → 로그파일 → OCR 3단계 폴백)
  - 카카오 좌표→주소 변환(Reverse Geocoding)
  - HEIC/HEIF → JPEG 변환, MOV 등 → MP4 자동 변환
- **실시간 Alert 피드**: 지도에서 신고 위치 확인, 위험도·거리·상태별 정렬, Socket.IO 기반 실시간 신규 신고/상태 업데이트 알림
- **마이페이지**: 내 신고 내역/처리 현황, 크래커 포인트, 관심지역 설정, 알림 설정, 회원 탈퇴
- **크래커 포인트 시스템**: 신고 처리 완료 시 포인트 적립(+20), 반려 시 차감(-10), 포인트 사용 내역(PointLog) 관리
- **크랙톡**: 실시간 커뮤니티 채팅(Socket.IO), 포인트 소모형 채팅 + 관리자 블라인드 처리
- **비속어 필터**: 닉네임/게시글/채팅에 특수문자 우회까지 차단하는 금칙어 필터
- **PWA 지원**: manifest.json + Service Worker 기반 설치 지원(인증 HTML/API는 네트워크 전용, 공개 정적 자산만 캐시)

### 관리자 기능
- **관리자 대시보드**: 긴급/오늘 접수/처리중/반려 등 탭별 신고 현황, 우선순위 스코어링(위험도·반복제보·장기미처리 가중치)
- **신고(Incident) 관리**: 상태별/위험도별/지역별 필터링, 검색, 정렬, 단건/일괄 상태 변경, AI 재분석 트리거
- **위치 기반 그룹핑**: 반경 50m·24시간 이내 중복 신고를 자동 그룹화하여 대표 신고로 통합 처리
- **회원 관리**: 권한(admin/manager/user) 변경, 계정 정지/해제, 회원별 신고 통계(처리율/반려율/중복률) 상세 조회
- **통계**: 지역별 계층 집계(시/도 → 시/군/구 → 동/읍/면), 기간별(7일/30일/전체) 추이 차트
- **공지사항** 등록 및 관리

### 리스크 방어 로직 (Technical Risk Mitigation)
| 리스크 | 방어 로직 |
|---|---|
| 어뷰징/포인트 파밍 (중복·허위 제보) | 동일 사용자 반경 50m·24시간 이내 중복 신고 자동 거절 |
| 개인정보 노출 | GPS 추출 후 EXIF 제거·UUID 파일명 변경·첨부 권한 검증을 수행하고, OpenCV 기반 얼굴/번호판 흐림 처리와 위치/미디어 자동 보관 만료 정책을 적용합니다. Haar 기반 마스킹은 누락 가능성이 있으므로 고위험 운영 환경에서는 전용 검출 모델과 관리자 검수가 필요합니다. |
| 공무집행방해/스팸 | MVP에서는 YOLOv8 AI 탐지 결과와 설정된 임계값을 기준으로 신고를 자동 분류하며, 기준을 충족하지 못한 신고는 관리자 확인 전에 반려 상태로 처리합니다. 오탐·미탐에 대비한 관리자 재검토·이의제기 절차는 추가 보완이 필요합니다. |

## 기술 스택

**Backend**
- Python 3 / Flask, Flask-SQLAlchemy
- Flask-SocketIO (eventlet) — 실시간 알림/채팅
- PyMySQL + certifi — MySQL 호환 DB 연결 (로컬 MySQL / TiDB Cloud 등)

**AI / Vision**
- Ultralytics YOLOv8 (YOLOv8s) — 포트홀/싱크홀 탐지
- OpenCV — 영상 프레임 분석 및 바운딩박스 오버레이 인코딩

**이미지/미디어 처리**
- Pillow, pillow_heif (HEIC/HEIF 변환), piexif, exifread — GPS/EXIF 추출 및 제거
- imageio-ffmpeg — 영상 코덱 변환(MOV/AVI → MP4)

**Frontend**
- Jinja2 템플릿, Bootstrap Icons, Chart.js
- Kakao Maps JavaScript SDK — 지도/좌표 변환
- PWA (Web App Manifest + Service Worker)

**Infra**
- MySQL 8.x 호환 DB (로컬 MySQL 기준으로 개발, TiDB Cloud 등 MySQL 호환 클라우드 DB로도 전환 가능)

## AI 모델

- **아키텍처**: YOLOv8s (Small)
- **최신 버전**: v6 Sinkhole Integration — 5종 핵심 클래스(Pothole / Major Crack / Minor Crack / Asset / Sinkhole)로 재매핑
- **데이터셋**: 총 215,278장 (train 193,848 / val 21,430, 약 9:1 비율)
- **성능 (v5 → v6)**:

  | 지표 | v5 (100 epoch) | v6 (18 epoch, 파인튜닝) |
  |---|---|---|
  | mAP50 | 0.8688 | **0.9017** (+3.8%) |
  | mAP50-95 | 0.6504 | 0.6528 |
  | Recall | 0.8075 | **0.8484** (+4.1%) |
  | Precision | 0.8296 | 0.8355 (+0.6%) |

- v5 가중치를 기반으로 싱크홀 데이터를 추가해 전이학습(AdamW, batch 32)한 결과입니다.
- 학습된 모델 가중치는 데이터셋 및 모델 재배포 조건 검토를 위해 공개 저장소에 포함하지 않았습니다. 저장소에는 모델 추론·서비스 연동 코드와 프로젝트 당시의 성능 결과만 포함됩니다.
- 학습 지표 원본 데이터: `static/training_analysis.json`, 학습 결과 시각화: `static/images/yolo/v5`, `static/images/yolo/v7`
  - `v7` 폴더명은 내부 산출물 관리 과정에서 붙은 이름이며, 위 표의 최신 모델 버전(v6, Sinkhole Integration)과 동일한 학습 결과를 담고 있습니다. 폴더명과 모델 버전명이 다른 이유는 저장소만으로 정확히 확인되지 않아 임의로 변경하지 않았습니다.

### 데이터 출처 및 공개 범위

학습에는 공개 도로 파손 데이터(AI-Hub 라벨링 데이터)와 팀에서 정리한 추가 데이터를 활용했습니다. v6 버전은 여기에 팀에서 별도로 확보한 싱크홀(Sinkhole) 데이터를 추가해 파인튜닝한 결과입니다. 원본 이미지와 라벨 데이터는 본 저장소에 포함하지 않았으며, 데이터셋별 정확한 이름·출처·라이선스는 저장소 내 자료만으로 확인되지 않아 임의로 명시하지 않았습니다. 실제 배포 또는 상업적 활용 전에는 데이터셋별 사용 조건을 별도로 확인해야 합니다.

### 성능 수치에 대한 참고사항

위 성능 수치는 프로젝트 진행 당시의 Validation 결과이며, 저장소에는 원본 학습 로그·데이터셋·가중치 파일이 포함되어 있지 않아 현재 저장소만으로 동일 수치를 완전히 재현할 수는 없습니다. `static/training_analysis.json`과 `static/images/yolo/` 내 시각화 이미지는 학습 당시 결과를 보여주는 참고 자료입니다.

## 프로젝트 구조

```
Crack_project/
├── app.py                    # Flask 엔트리포인트, AI 분석 파이프라인, 공통 라우트
├── extensions.py             # SQLAlchemy / SocketIO 확장 초기화
├── models.py                 # DB 모델 (Member, Report, AiResult, PointLog, ...)
├── utils.py                  # 비속어 필터, GPS/EXIF 추출, 거리 계산, 역지오코딩
├── migrate_db.py / rollback_db.py  # 명시적 확인이 필요한 MySQL DB 복사 도구 진입점
├── run_server.bat            # Windows 서버 실행 스크립트 (포트 9200)
├── requirements.txt
├── services/                 # 기능별 Blueprint
│   ├── auth_service.py       # 로그인/회원가입/아이디·비번 찾기
│   ├── report_service.py     # 파일 업로드, GPS 추출, 신고 제출
│   ├── alert_service.py      # 실시간 피드, 신고 상세/수정, 상태 변경
│   ├── status_service.py     # 내 신고 현황, 크랙톡
│   ├── my_service.py         # 마이페이지, 설정, 회원 탈퇴
│   ├── admin_service.py      # 관리자 대시보드/신고관리/회원관리/통계
│   └── region_service.py     # 행정구역 정규화/계층 파싱
├── templates/                # Jinja2 템플릿 (+ templates/ppt: 프로젝트 소개 PPT)
├── static/                   # 아이콘, manifest.json, sw.js, 학습 결과 시각화 (모델 가중치(.pt)는 포함하지 않음)
├── secrets.example/          # .env / kakao_js_key.txt / profanity.json 예시
└── secrets/                  # (gitignore) 실제 비밀키 - 직접 생성 필요
```

## 시작하기

### 1. 요구사항
- Python 3.10+
- MySQL 8.x 호환 DB (로컬 MySQL 또는 TiDB Cloud 등)
- Kakao Developers REST API 키 / JavaScript 키
- 학습된 YOLO 가중치 파일(저장소에는 포함되지 않음)

### 2. 설치

```bash
pip install -r requirements.txt
```

### 3. 비밀 설정 파일 생성

`secrets.example/` 폴더를 참고하여 `secrets/` 폴더를 만들고 아래 파일을 채워주세요.

```
secrets/
├── .env                # DB 접속 정보, Flask secret key, Kakao REST API 키
├── kakao_js_key.txt    # Kakao Maps JavaScript SDK 키
└── profanity.json      # 금칙어 목록 (hex 인코딩된 ko/en 배열)
```

`secrets/.env` 예시:

```
DB_USER=your_db_user
DB_PASSWORD=your_db_password
DB_HOST=your_db_host
DB_PORT=3306
DB_NAME=your_db_name
FLASK_SECRET_KEY=your_flask_secret_key_here
KAKAO_REST_API_KEY=your_kakao_rest_api_key_here
YOLO_MODEL_PATH=C:/absolute/path/to/best.pt
```

비밀번호 재설정 메일과 AI 작업 큐 설정은 `secrets.example/.env.example`의 SMTP/AI 항목을 참고하세요.

### 4. 서버 실행

```bash
python app.py
```

또는 Windows에서:

```bash
run_server.bat
```

기동 후 `http://127.0.0.1:9200` 으로 접속합니다. `run_server.bat`은 서버 시작 전에 `flask db upgrade`를 실행합니다. 수동 배포에서는 다음 명령으로 스키마를 먼저 적용하세요.

```bash
python -m flask --app app db upgrade
```

`/healthz`는 프로세스 생존 상태를, `/readyz`는 DB·AI·지도·금칙어·SMTP 구성 상태를 반환합니다. 운영에서 필수로 간주할 기능은 `.env`의 `REQUIRE_*` 설정으로 readiness 조건에 포함할 수 있습니다.

AI 작업은 `ai_jobs` 테이블에 저장되어 재시작 후 복구되며, 로그인·업로드 등의 요청 제한도 DB에 기록되어 여러 서버 프로세스에서 공유됩니다. `flask retention-run`으로 보관 정책을 즉시 실행할 수 있고, 기본적으로 삭제 미디어 7일, 반려 미디어 30일, 위치정보 365일을 보관합니다. 신고에 연결되지 않은 임시 업로드는 기본 24시간 뒤 제거됩니다.

### 운영 프로세스 분리

로컬 개발은 `APP_PROCESS_ROLE=all`로 웹·AI·보관 스케줄러를 한 프로세스에서 실행합니다. 운영 환경은 중복 실행과 모델 메모리 중복 적재를 막기 위해 다음처럼 분리해야 합니다.

```bash
# 웹 프로세스(여러 개 실행 가능)
APP_PROCESS_ROLE=web python app.py

# AI 작업자(필요한 수만큼 명시적으로 실행)
APP_PROCESS_ROLE=worker python -m flask --app app ai-worker

# 보관 정책 스케줄러(정확히 1개 실행)
APP_PROCESS_ROLE=scheduler python -m flask --app app retention-loop

# DB 백업 스케줄러(정확히 1개 실행)
APP_PROCESS_ROLE=scheduler python -m flask --app app backup-loop
```

`APP_ENV=production`에서는 실수로 통합 프로세스를 여러 개 실행하지 않도록 `APP_PROCESS_ROLE=all` 기동을 거부합니다.

### DB 백업

SQLite는 온라인 백업 API로, MySQL/MariaDB는 `mysqldump`의 단일 트랜잭션 방식으로 백업합니다. 백업과 SHA-256 파일은 기본적으로 Git에서 제외되는 `backups/`에 저장됩니다.

```bash
python -m flask --app app backup-create
python -m flask --app app backup-verify backups/백업파일.sqlite3
```

운영에서는 `BACKUP_DIRECTORY`를 별도 암호화 디스크나 접근 제한 저장소로 지정하고, 외부 스케줄러에서 `backup-create`를 정기 실행하세요.
또는 `backup-loop`를 전용 프로세스로 실행하면 기본 24시간마다 백업하고 30일이 지난 백업을 정리합니다.

빈 데이터베이스의 최초 관리자 계정은 비밀번호를 셸 기록에 남기지 않는 대화형 명령으로 생성합니다.

```bash
python -m flask --app app create-admin
```

## 데이터베이스 모델

| 모델 | 설명 |
|---|---|
| `Member` | 회원 정보 (계정, 닉네임, 크래커 포인트, 관심지역, 관리자 여부) |
| `Report` | 도로파손 신고 (위치, 첨부파일, 상태, 반려 사유) |
| `PasswordResetToken` | 해시된 일회용 비밀번호 재설정 토큰과 만료/사용 시각 |
| `AIJob` | 재시작 복구·재시도를 지원하는 영속 AI 분석 작업 |
| `RateLimitBucket` | 여러 프로세스가 공유하는 API 요청 제한 카운터 |
| `AiResult` | 신고별 AI 분석 결과 (손상 여부, 신뢰도, 손상 유형) |
| `VideoDetection` | 영상 프레임 단위 AI 검출 결과 (바운딩박스 좌표 포함) |
| `PointLog` | 크래커 포인트 적립/차감 내역 |
| `UserSettings` | 알림 등 사용자별 설정 |
| `Notice` | 공지사항 |
| `CrackTalk` | 실시간 커뮤니티 채팅 (블라인드 처리 지원) |

## 팀 소개

**TEAM CRACKER** — 풀스택 개발부터 비전학습(YOLO)까지 함께한 5인 팀

| 이름 | 역할 |
|---|---|
| 팀장 | 프로젝트 총괄 · 프론트 레이아웃 셋팅 · DB/백엔드 통합 · 비속어 필터 구현 |
| 팀원 A | 회원관련 CRUD · 아이디 중복 방지 로직 · 모델학습 |
| 팀원 B | 데이터 라벨링 · 디버깅 · 이미지 학습 데이터 확보 · 모델학습 |
| 이지건 (본인) | Alert 피드 · 관리자 대시보드 · 거리 기반 필터 로직 · 모델학습 |
| 팀원 C | Report/게시물 첨부 · 중복 신고 차단 로직 · 디버깅 · 데이터 라벨링 & 모델학습 |

> 본인을 제외한 팀원 이름은 개인정보 보호를 위해 익명화했습니다. 역할 설명은 원문을 유지했습니다.

## 개인 기여 범위

제가 맡아 직접 손댄 부분입니다. 팀 작업 중 제가 집중했던 건 아래 6가지입니다.

- **알림/신고 흐름**: 신고 목록 정렬·필터·그룹핑, 관리자 화면의 신고/회원 조회 흐름 정리
- **보안/안정성**: CSRF 처리, 업로드 미디어 검사, 민감 데이터 보호 경로 점검
- **AI 처리 분리**: AI 분석을 큐로 분리해 실패 시 재처리/복구가 되도록 정리
- **운영 점검**: /healthz, /readyz, /ops/metrics 추가 및 운영 상태 판단 기준 보완
- **데이터 정리 정책**: 보관 기간 설정, 삭제·반려·임시 업로드 파일 정리, 백업 생성/검증
- **회귀 테스트 추가**: `tests/test_p1.py`로 핵심 동작(보안 헤더, 권한, 미디어, 큐, 보관정책)을 최소한 체크

이외의 기능은 팀원들과 함께 작업한 내용이 더 많고, 여기에는 제가 직접 구현하지 않은 부분이 섞일 수 있습니다.

## 라이선스

팀이 작성한 애플리케이션 코드는 저장소의 [Apache License 2.0](LICENSE)을 따릅니다. 외부 라이브러리, 데이터셋, 모델 및 외부 서비스(Kakao Maps 등)는 각 제공자의 라이선스와 이용 조건을 따르며, 본 저장소에서 제외한 모델 가중치는 저장소 라이선스 적용 대상이 아닙니다.

## 운영 모니터링

- 관리자 권한으로 로그인한 계정에서 GET /ops/metrics를 호출하면 AI 작업 큐, 리포트 상태, 멤버 수, RateLimit 버킷 상태를 확인할 수 있습니다.
- /healthz : 프로세스 기동 상태, /readyz : 준비 상태 점검용으로 운영 자동화에 바로 사용할 수 있습니다.
