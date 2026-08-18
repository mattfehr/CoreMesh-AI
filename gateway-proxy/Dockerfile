# CoreMesh AI — Gateway Proxy (Go 1.22)
# Module: multi-stage gateway container build.
# Role: compiles the Go edge binary and packages it in a minimal runtime image.
# Dependencies: Go/Alpine base images, go.mod/go.sum, and gateway source.
# Side effects: downloads modules during build and produces an image exposing 8080.
FROM golang:1.22-alpine AS builder

WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download

COPY . .
RUN go build -o gateway ./cmd/main.go

# ---- runtime image ----
FROM alpine:3.20

WORKDIR /app
COPY --from=builder /app/gateway .

EXPOSE 8080
ENTRYPOINT ["/app/gateway"]
