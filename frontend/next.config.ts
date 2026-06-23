/** @type {import('next').NextConfig} */
const nextConfig = {
  // Standalone output for Docker — minimal production bundle
  output: 'standalone',

  // Rewrite /api/* requests to the backend.
  // - Server-side (SSR / Server Components): uses INTERNAL_API_URL (http://api:8000 in Docker)
  // - Browser client: uses NEXT_PUBLIC_API_URL (http://localhost:8000 via port mapping)
  async rewrites() {
    const internalApiUrl = process.env.INTERNAL_API_URL || 'http://localhost:8000';
    return [
      {
        source: '/api/:path*',
        destination: `${internalApiUrl}/api/:path*`,
      },
    ];
  },
};

export default nextConfig;
