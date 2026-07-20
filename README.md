# 물류 출고 변환 프로그램

현재 단계는 Supabase 로그인과 `items` 테이블 읽기 검증용 최소 실행본입니다.

1. `config.example.json`을 `config.json`으로 복사합니다.
2. Supabase의 공개용 `publishable` 또는 기존 `anon` 키를 입력합니다.
3. Python 3.11 이상에서 `pip install -r requirements.txt`를 실행합니다.
4. `python main.py`로 실행합니다.

비밀번호와 `service_role` 키는 파일에 저장하지 않습니다.
