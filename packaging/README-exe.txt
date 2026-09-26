coindata 실행 파일 (Windows)

1. 이 폴더를 원하는 곳(예: 문서\coindata)에 둔다.
2. coindata.exe를 두 번 눌러 실행한다.
   처음 실행할 때 "Windows의 PC 보호" 창이 뜨면 "추가 정보" → "실행"을 누른다.
   (서명 인증서가 없는 실행 파일이라 나오는 경고다.)
3. 처음이면 [초기 적재…]를 눌러 과거 데이터를 받는다. 기본 130일, 수십 분 걸릴 수 있다.
4. 이후에는 [요약 실행]으로 요약을 만들고 [복사]로 판단 모델에 붙여넣는다.

- 설정, 저장소(db 폴더), 요약(summaries 폴더)은 모두 coindata.exe 옆에 생긴다.
  폴더째 옮기거나 백업하면 된다.
- 설정을 바꾸려면 coindata.example.toml을 coindata.toml로 복사한 뒤 바꿀 값만 남긴다.
- 새 버전으로 바꿀 때는 coindata.exe만 교체한다. db, summaries, coindata.toml은 그대로 둔다.
