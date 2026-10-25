# Build the static site, then serve it with nginx (see deploy/).
FROM python:3.13-slim AS build
WORKDIR /app
COPY build.py ./
COPY src src
COPY deploy deploy
RUN python build.py && python deploy/nginx_conf.py > /nginx.conf

FROM nginx:1.27-alpine
ENV NGINX_ENTRYPOINT_QUIET_LOGS=1
COPY --from=build /nginx.conf /etc/nginx/nginx.conf
COPY deploy/auth.js /etc/nginx/auth.js
COPY --from=build /app/dist /usr/share/nginx/html
