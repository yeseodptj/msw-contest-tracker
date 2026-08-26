# MSW Contest Tracker 배포 순서

1. 이 폴더 안의 파일과 폴더를 GitHub 저장소 루트에 전부 업로드합니다.
2. GitHub 저장소의 Actions → Collect MSW contest data → Run workflow를 눌러 첫 수집을 실행합니다.
3. 성공하면 저장소 루트에 tracker.db가 생성/갱신됩니다.
4. Streamlit Community Cloud에서 저장소를 연결하고 Main file path를 app.py로 지정해 배포합니다.
5. 이후 GitHub Actions가 매시간 자동 수집합니다.

UI만 수정할 때는 app.py만 수정하면 됩니다. 재설치나 전체 재수집은 필요 없습니다.
