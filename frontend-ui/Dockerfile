# CoreMesh React frontend production image.
# Role: compiles the Vite SPA and serves it from Nginx on port 3000.
# Dependencies: Node/npm registry during build and the checked-in lockfile.
# Side effects: build downloads npm packages; runtime opens an HTTP listener.
FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
ARG VITE_GATEWAY_BASE_URL=http://localhost:8080
ENV VITE_GATEWAY_BASE_URL=${VITE_GATEWAY_BASE_URL}
RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 3000
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD wget -qO- http://127.0.0.1:3000/ >/dev/null 2>&1 || exit 1

CMD ["nginx", "-g", "daemon off;"]
