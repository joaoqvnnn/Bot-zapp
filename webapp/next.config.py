// ============================================================
// Larizinha Store — WebApp Next.js (config)
// ============================================================

/** @type {import('next').NextConfig} */
const nextConfig = {
  // ---------- Modo estrito ----------
  reactStrictMode: true,

  // ---------- Build standalone (usado no Dockerfile) ----------
  output: "standalone",

  // ---------- Compressão ----------
  compress: true,

  // ---------- Otimizações ----------
  swcMinify: true,

  // ---------- Ignora erros de ESLint no build ----------
  // (mantemos o lint rodando localmente via `npm run lint`)
  eslint: {
    ignoreDuringBuilds: true,
  },

  // ---------- Ignora erros de TypeScript no build ----------
  // (o typecheck roda localmente via `npm run typecheck`)
  typescript: {
    ignoreBuildErrors: false, // deixa o build falhar se houver erro de tipo
  },

  // ---------- Imagens remotas permitidas ----------
  images: {
    remotePatterns: [
      {
        protocol: "https",
        hostname: "**",
      },
      {
        protocol: "http",
        hostname: "localhost",
      },
    ],
    formats: ["image/avif", "image/webp"],
  },

  // ---------- Headers de segurança ----------
  async headers() {
    return [
      {
        source: "/(.*)",
        headers: [
          { key: "X-Frame-Options", value: "SAMEORIGIN" },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          {
            key: "Permissions-Policy",
            value: "camera=(), microphone=(), geolocation=()",
          },
        ],
      },
    ];
  },

  // ---------- Redirects ----------
  async redirects() {
    return [
      {
        source: "/shop",
        destination: "/",
        permanent: false,
      },
    ];
  },

  // ---------- Env expostas ao client (NEXT_PUBLIC_*) ----------
  env: {
    NEXT_PUBLIC_APP_NAME: process.env.NEXT_PUBLIC_STORE_NAME || "Larizinha Store",
    NEXT_PUBLIC_BOT: process.env.NEXT_PUBLIC_BOT_USERNAME || "",
    NEXT_PUBLIC_API: process.env.NEXT_PUBLIC_API_URL || "",
  },

  // ---------- Webpack tweaks ----------
  webpack: (config, { isServer }) => {
    // Ignora módulos do Node no client
    if (!isServer) {
      config.resolve.fallback = {
        ...config.resolve.fallback,
        fs: false,
        net: false,
        tls: false,
      };
    }
    return config;
  },

  // ---------- Experimental ----------
  experimental: {
    optimizePackageImports: [
      "lucide-react",
      "@radix-ui/react-dialog",
      "@radix-ui/react-dropdown-menu",
    ],
  },
};

module.exports = nextConfig;
