# frontend/Dockerfile
# ---------------------
# React 정적 빌드 + Nginx 서빙용 이미지(운영 환경).
#
# 1단계(build): npm ci && npm run build로 frontend/dist에 정적 파일을 만든다.
# 2단계(runtime): nginx:alpine이 그 정적 파일만 복사해 서빙한다 - React는
# 빌드 산출물(정적 파일)만 필요하므로 운영 이미지에 Node.js를 포함시키지 않는다.
#
# /api, /health 요청은 nginx.conf가 그대로 api 컨테이너(FastAPI, 8000번 포트)로
# 리버스 프록시한다(dev 환경의 vite proxy와 동일한 역할).

FROM node:20-alpine AS build
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci
COPY . .
RUN npm run build

FROM nginx:1.27-alpine
COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
