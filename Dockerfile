FROM php:8.3-cli-alpine
RUN apk add --no-cache inotify-tools

COPY vendor /app/vendor
COPY config.php /app/config.php
COPY orz.php /usr/local/bin/orz
COPY listen.sh /usr/local/bin/listen
COPY org.ini /usr/local/etc/php/conf.d/org.ini

WORKDIR /app

CMD ["sh", "-c", "listen"]
