FROM postgres:16-alpine AS database
COPY --chmod=0644 db/001_schema.sql /docker-entrypoint-initdb.d/001_schema.sql
COPY --chmod=0644 deploy/local-workspace/empty.sql /docker-entrypoint-initdb.d/002_empty.sql

FROM nginx:1.27-alpine AS website
COPY frontend/ /usr/share/nginx/html/
RUN chmod -R a+rX /usr/share/nginx/html
COPY --chmod=0644 frontend/nginx.conf /etc/nginx/conf.d/default.conf
