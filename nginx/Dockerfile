FROM nginx:1.27-alpine

# The upstream nginx:alpine image no longer ships wget or curl in its final
# layer (it's built FROM nginx:*-alpine-slim, and both are purged as build
# deps — see nginxinc/docker-nginx's alpine-slim Dockerfile). This service's
# HEALTHCHECK (docker-compose.yml) needs something inside the container to
# make an HTTP request against itself; curl is the smallest reliable addition.
RUN apk add --no-cache curl
