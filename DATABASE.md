# Supabase 데이터 구조

프로그램이 현재 참조하는 테이블은 다음과 같다. 실제 컬럼과 RLS 정책은 Supabase 프로젝트를 기준으로 확인한다.

| 테이블 | 용도 | 주요 필드 예시 |
|---|---|---|
| `items` | 표준 재고 품목 | `item_code`, `standard_name`, `model`, `color`, `form`, `is_active` |
| `registered_products` | 판매처 등록상품/세트상품 | `registered_product_id`, `original_name`, `normalized_name`, `is_active` |
| `product_components` | 세트상품 구성품 | `registered_product_id`, `item_code`, `quantity`, `sequence` |
| `item_barcodes` | 면세점 바코드와 품목 연결 | `barcode`, `item_code`, `is_active` |
| `duty_free_locations` | 면세점/매장 정보 | 면세점명, 매장코드 등 프로젝트 정의 필드 |
| `item_aliases` | 판매처 상품명 수동 확정값 | `source_channel`, `normalized_source`, `components`, `is_active` |
| `shipment_history` | 중복 출고 방지 이력 | `duplicate_key`, 주문·수령·상품 관련 필드 |
| `app_user_roles` | 사용자 권한 | 사용자 식별자, `app_role` |
| `ecount_employees` | 이카운트 담당자 | `employee_code`, `employee_name` |
| `ecount_warehouses` | 이카운트 창고 | `warehouse_code`, `warehouse_name` |

## 데이터 관리 원칙

- 새로운 판매처 표현이 기존 품목과 같은 결과라면 `item_aliases`로 연결한다.
- 실제로 새로운 재고 단품이면 `items`에 추가한다.
- 새로운 세트면 `registered_products`와 `product_components`를 함께 추가한다.
- 면세점 바코드는 `item_barcodes`에 표준 품목코드와 연결한다.
- 코드 값은 문자열로 취급하여 앞자리 0을 보존한다.
- 대량 반영 전에는 중복 코드와 비활성 품목을 점검한다.

## 보안

- 클라이언트에는 공개용 Supabase publishable/anon 키만 사용한다.
- `service_role` 키는 클라이언트 프로그램과 GitHub에 절대 저장하지 않는다.
- 테이블 수정 권한은 UI뿐 아니라 Supabase RLS에서도 제한한다.
