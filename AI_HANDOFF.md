# AI 작업 인수인계

이 저장소는 리큐엠의 출고 업무에서 휴먼에러를 줄이기 위한 Windows 데스크톱 프로그램이다. 새로운 AI나 개발자는 작업을 시작하기 전에 다음 문서를 순서대로 읽는다.

1. `PROJECT_CONTEXT.md`
2. `BUSINESS_RULES.md`
3. `DATABASE.md`
4. `ECOUT_API.md`
5. `CHANGELOG.md`
6. `NEXT_TASKS.md`

## 현재 기준 상태

- 기준 버전: 1.0.14
- 작업 브랜치: `agent/ecount-transfer-integrated-1.0.5`
- 기본 브랜치: `main`
- GUI: Python + PySide6
- 데이터베이스 및 인증: Supabase
- Excel: openpyxl, xlrd
- PDF: pdfplumber
- 실행파일: PyInstaller

## 작업 원칙

- 기존 B2C/B2B 자동 판별, 품목 매칭, 수동 수정, 합포장/중복 판정 기능을 보존한다.
- 확실하지 않은 품목을 자동 확정하지 않고 `similar`, `ambiguous`, `missing`으로 표시한다.
- 이카운트 실제 전표 등록 전에는 반드시 사용자 최종 확인을 받는다.
- 고객 이름, 전화번호, 주소, 주문 파일을 저장소에 커밋하지 않는다.
- `config.json`, 비밀번호, Supabase `service_role`, 이카운트 API 인증키를 커밋하지 않는다.
- 새로운 DB 테이블이나 컬럼을 가정하지 말고 Supabase 스키마를 확인한다.
- 기능 변경 후 Python 구문 검사, 대표 Excel/PDF 분석, EXE 빌드 검증을 수행한다.

## 다른 AI에 전달할 시작 문구

```text
저장소의 AI_HANDOFF.md와 그 문서에서 안내하는 파일을 순서대로 읽어라.
현재 구현과 업무 규칙을 유지하면서 요청된 변경만 수행하고,
인증정보와 고객 개인정보는 코드·문서·커밋에 포함하지 마라.
```
