# 多阶段构建：静态编译 Go 服务，运行镜像只含二进制。
FROM golang:1.22-alpine AS build
WORKDIR /src
ENV GOFLAGS=-mod=mod GOPROXY=https://goproxy.cn,direct CGO_ENABLED=0
COPY go-service/go.mod go-service/go.sum ./
RUN go mod download
COPY go-service/ ./
RUN go build -trimpath -ldflags "-s -w" -o /out/api ./cmd/api

FROM alpine:3.20
RUN apk add --no-cache ca-certificates tzdata && adduser -D -u 10001 app
WORKDIR /app
COPY --from=build /out/api /app/api
RUN mkdir -p /uploads && chown -R app /uploads
USER app
ENV UPLOAD_DIR=/uploads PORT=8081
EXPOSE 8081
ENTRYPOINT ["/app/api"]
