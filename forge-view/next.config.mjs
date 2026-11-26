/** @type {import('next').NextConfig} */
// The pages read /api/*, which is proxied server-side so the browser only ever
// talks to this origin. By default that is the public read-only API behind the
// live demo; point API_ORIGIN at another deployment of the backend to change it.
const API = process.env.API_ORIGIN || "https://agentic-research-trading.vercel.app";

const nextConfig = {
  reactStrictMode: true,
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API}/api/:path*` }];
  },
};
export default nextConfig;
