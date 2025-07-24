# 1. 베이스 이미지 선택
FROM python:3.11-slim

# 2. 한글 파일명을 위한 UTF-8 환경 설정
ENV LANG C.UTF-8
ENV LC_ALL C.UTF-8

# 3. 컨테이너 안의 작업 폴더 설정
WORKDIR /app

# 4. 필요한 라이브러리 목록 복사 및 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. 프로젝트의 모든 파일을 컨테이너 안으로 복사
COPY . .

# --- ▼▼ 권한 문제 해결을 위한 부분 ▼▼ ---

# 6. 애플리케이션을 실행할 non-root 사용자 생성
RUN addgroup --system nonroot && adduser --system --ingroup nonroot nonroot

# 7. 데이터 폴더 생성 및 non-root 사용자에게 권한 부여
# Render의 영구 디스크 경로(/var/data)와 앱 코드 폴더(/app) 모두에 권한을 줍니다.
RUN mkdir -p /var/data && chown -R nonroot:nonroot /var/data /app

# 8. 이후 모든 명령어를 non-root 사용자로 실행하도록 전환
USER nonroot

# --- ▲▲ 여기까지 추가/수정 ▲▲ ---

# 9. 컨테이너가 5000번 포트를 사용한다고 알림
EXPOSE 5000

# 10. 컨테이너가 시작될 때 실행할 최종 명령어
CMD ["gunicorn", "--bind", "0.0.0.0:5000", "app:app"]