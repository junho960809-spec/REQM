# REQM 출고 관리 프로그램 1.0.4

셀메이트·면세점 주문 파일을 자동 판별하고, Supabase 품목 DB와 대조하여 택배 출고용 엑셀을 생성하는 Windows 설치형 프로그램입니다.

## 주요 기능

- B2C/B2B 입력 양식 자동 판별
- 품목명·품목코드 매칭과 유사/미등록 품목 표시 및 수동 수정
- 합포장과 실제 중복 출고 구분
- 관리자 전용 품목 DB 관리
- 이카운트 창고이동 보드
- Excel/PDF 품목 분석 및 이카운트 품목코드 매칭
- 담당자·보내는 창고·받는 창고 선택 또는 코드 직접 입력
- 이카운트 `SaveLocationTran` API 등록 전 최종 확인

## 실행 준비

1. `config.example.json`을 `config.json`으로 복사합니다.
2. Supabase `publishable` 또는 기존 `anon` 키를 입력합니다.
3. Python 3.11 이상에서 `pip install -r requirements.txt`를 실행합니다.
4. `python main.py`로 실행합니다.

이카운트 API 인증키는 프로그램 실행 중에만 입력하며 파일에 저장하지 않습니다. Supabase 비밀번호, `service_role` 키, 이카운트 API 인증키는 GitHub에 커밋하지 마세요.

## 실행파일 빌드

```powershell
pyinstaller --noconfirm --clean REQM_1_0_4.spec
```
