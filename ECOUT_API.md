# 이카운트 API 연동

## 현재 연동

- ZONE 조회 후 회사의 ZONE을 사용한다.
- 로그인: `POST https://oapi{ZONE}.ecount.com/OAPI/V2/OAPILogin`
- 창고이동: `POST https://oapi{ZONE}.ecount.com/OAPI/V2/Others/SaveLocationTran?SESSION_ID={SESSION_ID}`
- 구현 파일: `ecount_transfer.py`

## 창고이동 주요 필드

| 필드 | 의미 |
|---|---|
| `IO_DATE` | 전표 일자 `yyyyMMdd` |
| `UPLOAD_SER_NO` | 요청 내 순번 |
| `EMP_CD` | 담당자 코드 |
| `WH_CD_F` | 보내는 창고 코드 |
| `WH_CD_T` | 받는 창고 코드 |
| `PROD_CD` | 이카운트 품목코드 |
| `PROD_DES` | 품목명 |
| `QTY` | 이동 수량 |
| `REMARKS` | 적요 |

## UI 동작

- 담당자와 창고는 엔터 또는 더블클릭으로 DB 목록을 연다.
- 코드를 직접 입력하면 일치하는 코드와 이름을 자동 표시한다.
- 일자는 달력에서 선택한다.
- API 인증키는 우측 상단 정보창에서 입력하며 프로그램 종료 시 폐기한다.
- 보내는 창고와 받는 창고가 같으면 등록을 막는다.
- 실제 등록 직전 사용자 확인창을 표시한다.

## 성공 판정

- HTTP/응답 `Status`만으로 성공을 확정하지 않는다.
- `Data.SuccessCnt`, `Data.FailCnt`, `Data.ResultDetails`, `Data.SlipNos`를 함께 확인한다.
- 검증 오류도 `Status: 200`으로 반환될 수 있으므로 `FailCnt`가 0인지 확인한다.

## 보안과 운영

- API 인증키와 SESSION_ID를 로그, 화면 캡처, GitHub에 남기지 않는다.
- 테스트 시 테스트 URL과 소량 품목을 먼저 사용한다.
- 등록 전 담당자·창고·품목코드·수량을 최종 검토한다.
