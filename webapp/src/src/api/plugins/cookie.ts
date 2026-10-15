/**
 * Larizinha Store — API TypeScript
 * Plugin Cookie — habilita cookies (usado para tokens do WebApp).
 */
import cookie from "@fastify/cookie";
import fp from "fastify-plugin";

import env from "@/config/env";

async function plugin(
  app: import("fastify").FastifyInstance,
): Promise<void> {
  await app.register(cookie, {
    secret: env.WEBAPP_SECRET || env.BOT_TOKEN,
    parseOptions: {},
    hook: "onRequest",
  });
}

export default fp(plugin, { name: "cookie-plugin" });
