# 물류 출고 변환 프로그램

현재 단계는 Supabase 로그인과 `items` 테이블 읽기 검증용 최소 실행본입니다.

1. `config.example.json`을 `config.json`으로 복사합니다.
2. Supabase의 공개용 `publishable` 또는 기존 `anon` 키를 입력합니다.
3. Python 3.11 이상에서 `pip install -r requirements.txt`를 실행합니다.
4. `python main.py`로 실행합니다.

비밀번호와 `service_role` 키는 파일에 저장하지 않습니다.

## 판매전표 반자동화 테스트 앱

`ecount_sales_app.py`는 스마트스토어 원본 주문을 품목/세트 DB와 매칭해 이카운트 판매전표 Excel을 생성합니다.

1. `python ecount_sales_app.py`로 실행합니다.
2. 필요하면 Supabase 이메일과 비밀번호로 최신 기준 DB를 불러옵니다. 비밀번호는 저장하지 않습니다.
3. 스마트스토어 원본 Excel, 주문 대상일, 전표 일자를 선택합니다.
4. `분석 및 자동 매칭`을 누르고 `확인 필요` 탭의 예외를 검토합니다.
5. 자동 변환 결과에서 수량·단가·창고를 수정한 뒤 `이카운트 Excel 저장`을 누릅니다.

기본값은 담당자 `00109`, 출하창고 `300`이며 QM4100은 `100` 본사창고로 자동 전환됩니다. 세트의 쿠폰·포인트 차액은 첫 번째 구성품인 본품에 반영하고 옵션품목 단가는 유지합니다.
