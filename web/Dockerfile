# Фронтенд: Next.js (UI + прокси /api → бэкенд).
FROM node:20-slim

WORKDIR /app

# Зависимости отдельным слоем (кэш по package*.json).
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

# Исходники и прод-сборка.
COPY . .
RUN npm run build

# Next проксирует /api на бэкенд по имени docker-сервиса.
ENV BACKEND_URL=http://backend:8000 \
    NODE_ENV=production

EXPOSE 3000
CMD ["npm", "run", "start"]
