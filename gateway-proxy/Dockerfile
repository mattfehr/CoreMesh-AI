# CoreMesh AI — Gateway Proxy (Go 1.22)
FROM golang:1.22-alpine AS builder

WORKDIR /app
COPY go.mod ./
RUN go mod download

COPY . .
RUN go build -o gateway ./cmd/main.go

# ---- runtime image ----
FROM alpine:3.20

WORKDIR /app
COPY --from=builder /app/gateway .

EXPOSE 8080
ENTRYPOINT ["/app/gateway"]
