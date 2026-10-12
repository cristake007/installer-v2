FROM php:8.4-fpm-alpine
RUN apk add --no-cache libpq && apk add --no-cache --virtual .build-deps $PHPIZE_DEPS postgresql-dev && docker-php-ext-install pdo_pgsql && apk del .build-deps
COPY public/ /var/www/html/public/
COPY hooks/ /opt/installer/
ARG FIXTURE_VERSION=1
RUN echo "$FIXTURE_VERSION" > /var/www/html/public/version.txt
ARG FAIL_COMPATIBILITY=0
ARG FAIL_HEALTH=0
RUN echo "$FAIL_COMPATIBILITY" > /opt/installer/incompatible && echo "$FAIL_HEALTH" > /opt/installer/unhealthy
ARG FAIL_MIGRATION=0
RUN echo "$FAIL_MIGRATION" > /opt/installer/fail
WORKDIR /var/www/html
