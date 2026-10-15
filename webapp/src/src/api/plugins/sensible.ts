/**
 * Larizinha Store — API TypeScript
 * Plugin Sensible — helpers úteis (httpErrors, assert, etc).
 */
import sensible from "@fastify/sensible";
import fp from "fastify-plugin";

async function plugin(
  app: import("fastify").FastifyInstance,
): Promise<void> {
  await app.register(sensible);
}

export default fp(plugin, { name: "sensible-plugin" });
