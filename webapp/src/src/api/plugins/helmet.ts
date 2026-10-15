/**
 * Larizinha Store — API TypeScript
 * Plugin Helmet — headers de segurança.
 */
import helmet from "@fastify/helmet";
import fp from "fastify-plugin";

async function plugin(
  app: import("fastify").FastifyInstance,
): Promise<void> {
  await app.register(helmet, {
    global: true,
    contentSecurityPolicy: false,
    crossOriginEmbedderPolicy: false,
    crossOriginResourcePolicy: { policy: "cross-origin" },
  });
}

export default fp(plugin, { name: "helmet-plugin" });
