"""단일 실행 파일의 진입 스크립트 (PRD FR-8.9). PyInstaller가 이 파일에서 시작한다."""

from coindata.gui import main

raise SystemExit(main())
