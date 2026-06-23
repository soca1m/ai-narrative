# Фронтенд: Next.js (UI + прокси /api → бэкенд).
FROM node:20-slim

WORKDIR /app

# Зависимости отдельным слоем (кэш по package*.json).
COPY package.json package-lock.json* ./
RUN npm install --no-audit --no-fund

# Next «запекает» rewrites в манифест на этапе build → BACKEND_URL должен быть
# задан ДО сборки, иначе прокси /api уйдёт на дефолт 127.0.0.1:8000 (в себя).
ENV BACKEND_URL=http://backend:8000 \
    NODE_ENV=production

# Исходники и прод-сборка.
COPY . .
RUN npm run build

EXPOSE 3000
CMD ["npm", "run", "start"]
