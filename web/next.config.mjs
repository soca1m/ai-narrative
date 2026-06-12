/** @type {import('next').NextConfig} */
// Бэкенд внутри WSL (не светим наружу). Браузер ходит на тот же origin (Next),
// а Next проксирует /api/* на FastAPI 127.0.0.1:8000 (server-to-server).
const BACKEND = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

const nextConfig = {
  reactStrictMode: false, // избегаем двойного открытия SSE в dev
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${BACKEND}/api/:path*` }];
  },
};
export default nextConfig;
