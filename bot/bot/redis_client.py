"""
Larizinha Store — Cliente Redis + Lock Manager
Responsável por:
- Conexão Redis assíncrona (singleton)
- Cache genérico com TTL
- Travas de concorrência REAIS:
    * product:{id}:lock   → reserva de estoque durante PIX/compra
    * user:{id}:view      → lock de "usuário está vendo o produto X"
    * user:{id}:action    → lock de ação única (evita duplo clique)
- Heartbeat dos usuários online
- Rate-limit / throttle por usuário
- Fila de tarefas leves (automações)
- Pub/Sub (opcional para notificações em tempo real)
"""
from __future__ import annotations

import asyncio
import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator, Optional

import redis.asyncio as aioredis
from redis.asyncio.client import Pipeline, Redis
from redis.exceptions import RedisError

from bot.config import settings


# ============================================================
# 🔧 CONSTANTES DE CHAVE
# ============================================================
class Keys:
    """Namespace central de chaves Redis."""

    PREFIX = settings.REDIS_PREFIX

    # ---- Concorrência / Travas ----
    PRODUCT_LOCK = "product:{pid}:lock"           # reserva de estoque
    PRODUCT_VIEW = "product:{pid}:views"          # contador de views ao vivo
    USER_VIEW = "user:{uid}:view"                 # produto que o user está olhando
    USER_ACTION = "user:{uid}:action:{name}"      # ação única (duplo clique)

    # ---- Sessões de fluxo (PIX, saque, gift, etc.) ----
    FLOW = "flow:{uid}:{name}"                    # estado do fluxo
    WITHDRAW_STATE = "withdraw:{uid}:state"       # saque em andamento
    PIX_STATE = "pix:{uid}:state"                 # PIX em andamento

    # ---- Cache ----
    CACHE = "cache:{name}:{key}"

    # ---- Presença / heartbeat ----
    ONLINE = "online:{uid}"
    ONLINE_SET = "online:set"

    # ---- Rate-limit / Throttle ----
    THROTTLE = "throttle:{uid}:{action}"

    # ---- Filas ----
    QUEUE_NOTIFY = "queue:notify"
    QUEUE_AUTOMATION = "queue:automation"

    # ---- Carteira de views por produto (dashboards em tempo real) ----
    DASHBOARD = "dashboard:{name}"

    @classmethod
    def k(cls, tpl: str, **kw: Any) -> str:
        """Formata uma chave com namespace."""
        return f"{cls.PREFIX}:{tpl.format(**kw)}"


# ============================================================
# 🔌 CLIENTE REDIS (singleton)
# ============================================================
_client: Optional[Redis] = None


async def get_redis() -> Redis:
    """Retorna o cliente Redis assíncrono (singleton)."""
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
            socket_keepalive=True,
            retry_on_timeout=True,
            max_connections=50,
        )
    return _client


async def close_redis() -> None:
    """Fecha o cliente Redis."""
    global _client
    if _client is not None:
        try:
            await _client.aclose()
        except Exception:
            pass
    _client = None


async def ping() -> bool:
    """Healthcheck Redis."""
    try:
        r = await get_redis()
        return bool(await r.ping())
    except Exception:
        return False


# ============================================================
# 💾 CACHE GENÉRICO
# ============================================================
async def cache_set(name: str, key: str, value: Any, ttl: int = 300) -> None:
    """Salva valor JSON no cache."""
    r = await get_redis()
    redis_key = Keys.k(Keys.CACHE, name=name, key=key)
    await r.set(redis_key, json.dumps(value, default=str), ex=ttl)


async def cache_get(name: str, key: str) -> Optional[Any]:
    """Lê valor JSON do cache."""
    r = await get_redis()
    redis_key = Keys.k(Keys.CACHE, name=name, key=key)
    raw = await r.get(redis_key)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return raw


async def cache_delete(name: str, key: str) -> None:
    r = await get_redis()
    await r.delete(Keys.k(Keys.CACHE, name=name, key=key))


async def cache_clear_namespace(name: str) -> int:
    """Remove todas as chaves de um namespace de cache."""
    r = await get_redis()
    pattern = Keys.k(Keys.CACHE, name=name, key="*")
    deleted = 0
    async for key in r.scan_iter(match=pattern, count=200):
        await r.delete(key)
        deleted += 1
    return deleted


# ============================================================
# 🔒 LOCK MANAGER — TRAVAS DE CONCORRÊNCIA REAIS
# ============================================================
class LockError(Exception):
    """Erro genérico de lock."""


class LockManager:
    """
    Gerencia as travas de concorrência REAIS.

    Fluxo:
      • Usuário abre detalhes do produto → `acquire_view` marca
        user:{uid}:view = product_id e, opcionalmente, reserva 1 estoque
        em product:{pid}:lock com TTL configurável.
      • Se outro usuário tentar comprar o mesmo produto e o estoque estiver
        reservado, é bloqueado até liberar (expira ou paga).
      • Ao pagar → `commit_lock` transforma a reserva em venda.
      • Ao expirar → `release_lock` libera.
    """

    # ---------------- VIEW ----------------
    @staticmethod
    async def acquire_view(user_id: int, product_id: int, ttl: Optional[int] = None) -> bool:
        """
        Marca que o usuário está visualizando o produto.
        Retorna False se o usuário já está em outra view (evita paralelo).
        """
        r = await get_redis()
        key = Keys.k(Keys.USER_VIEW, uid=user_id)
        ttl = ttl or settings.USER_VIEW_TTL
        current = await r.get(key)
        if current and current != str(product_id):
            return False
        await r.set(key, str(product_id), ex=ttl)

        # incrementa views "ao vivo"
        await r.hincrby(Keys.k(Keys.PRODUCT_VIEW, pid=product_id), "live", 1)
        await r.expire(Keys.k(Keys.PRODUCT_VIEW, pid=product_id), ttl)
        return True

    @staticmethod
    async def get_view(user_id: int) -> Optional[int]:
        r = await get_redis()
        raw = await r.get(Keys.k(Keys.USER_VIEW, uid=user_id))
        return int(raw) if raw else None

    @staticmethod
    async def release_view(user_id: int) -> None:
        r = await get_redis()
        key = Keys.k(Keys.USER_VIEW, uid=user_id)
        pid = await r.get(key)
        await r.delete(key)
        if pid:
            await r.hincrby(Keys.k(Keys.PRODUCT_VIEW, pid=pid), "live", -1)

    # ---------------- PRODUCT LOCK ----------------
    @staticmethod
    async def reserve_stock(
        product_id: int,
        user_id: int,
        quantity: int = 1,
        ttl: Optional[int] = None,
    ) -> Optional[str]:
        """
        Reserva `quantity` unidades do produto para o usuário.
        Retorna um token único se conseguiu; None se já está travado por outro.

        A chave fica no formato: product:{pid}:lock = {token:user_id:qty}
        """
        if not settings.ENABLE_CONCURRENCY_LOCK:
            return "disabled"

        r = await get_redis()
        lock_key = Keys.k(Keys.PRODUCT_LOCK, pid=product_id)
        ttl = ttl or settings.PRODUCT_LOCK_TTL
        token = uuid.uuid4().hex

        payload = json.dumps(
            {
                "token": token,
                "user_id": user_id,
                "quantity": quantity,
                "created_at": int(time.time()),
            }
        )

        # SET NX EX — atômico
        ok = await r.set(lock_key, payload, ex=ttl, nx=True)
        if ok:
            return token

        # Já existe lock. Verifica se é do próprio usuário (reentrada)
        raw = await r.get(lock_key)
        if raw:
            try:
                data = json.loads(raw)
            except Exception:
                return None
            if int(data.get("user_id", 0)) == user_id:
                return data.get("token")
        return None

    @staticmethod
    async def get_lock(product_id: int) -> Optional[dict]:
        r = await get_redis()
        raw = await r.get(Keys.k(Keys.PRODUCT_LOCK, pid=product_id))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    @staticmethod
    async def extend_lock(product_id: int, ttl: int) -> bool:
        r = await get_redis()
        return bool(await r.expire(Keys.k(Keys.PRODUCT_LOCK, pid=product_id), ttl))

    @staticmethod
    async def commit_lock(product_id: int, user_id: int) -> bool:
        """
        Transforma a reserva em venda: remove o lock.
        (A venda/baixa real de estoque é feita no banco de dados.)
        """
        r = await get_redis()
        lock_key = Keys.k(Keys.PRODUCT_LOCK, pid=product_id)
        raw = await r.get(lock_key)
        if not raw:
            return False
        try:
            data = json.loads(raw)
        except Exception:
            return False
        if int(data.get("user_id", 0)) != user_id:
            return False
        await r.delete(lock_key)
        return True

    @staticmethod
    async def release_lock(product_id: int, user_id: Optional[int] = None) -> bool:
        """
        Libera a reserva. Se `user_id` for passado, só libera se for o dono.
        """
        r = await get_redis()
        lock_key = Keys.k(Keys.PRODUCT_LOCK, pid=product_id)
        if user_id is None:
            return bool(await r.delete(lock_key))
        raw = await r.get(lock_key)
        if not raw:
            return False
        try:
            data = json.loads(raw)
        except Exception:
            return False
        if int(data.get("user_id", 0)) != user_id:
            return False
        return bool(await r.delete(lock_key))

    @staticmethod
    async def list_active_locks() -> list[dict]:
        """Lista todas as travas ativas (usado no painel admin)."""
        r = await get_redis()
        pattern = Keys.k(Keys.PRODUCT_LOCK, pid="*")
        results: list[dict] = []
        async for key in r.scan_iter(match=pattern, count=200):
            raw = await r.get(key)
            ttl = await r.ttl(key)
            if not raw:
                continue
            try:
                data = json.loads(raw)
            except Exception:
                continue
            data["ttl"] = ttl
            data["key"] = key
            # extrai o product_id da chave
            try:
                data["product_id"] = int(key.split(":")[-2])
            except Exception:
                data["product_id"] = None
            results.append(data)
        return results

    # ---------------- USER ACTION ----------------
    @staticmethod
    async def acquire_action(user_id: int, action: str, ttl: int = 30) -> bool:
        """
        Marca uma ação única por usuário (evita duplo clique).
        """
        r = await get_redis()
        key = Keys.k(Keys.USER_ACTION, uid=user_id, name=action)
        ok = await r.set(key, "1", ex=ttl, nx=True)
        return bool(ok)

    @staticmethod
    async def release_action(user_id: int, action: str) -> None:
        r = await get_redis()
        await r.delete(Keys.k(Keys.USER_ACTION, uid=user_id, name=action))

    @staticmethod
    @asynccontextmanager
    async def action(user_id: int, name: str, ttl: int = 30) -> AsyncGenerator[bool, None]:
        """
        Context manager para ações únicas:
            async with LockManager.action(uid, "buy"):
                ...
        """
        acquired = await LockManager.acquire_action(user_id, name, ttl)
        try:
            yield acquired
        finally:
            if acquired:
                await LockManager.release_action(user_id, name)


# ============================================================
# 🧭 ESTADOS DE FLUXO (PIX, Saque, Gift)
# ============================================================
class FlowState:
    """
    Guarda estado de fluxo do usuário em Redis com TTL.
    Ex.: saque → {"step": "password", "chave": "...", "valor": 50.00, "txid": "..."}
    """

    DEFAULT_TTL = 15 * 60  # 15 minutos

    @staticmethod
    async def set(
        user_id: int,
        name: str,
        data: dict,
        ttl: int = DEFAULT_TTL,
    ) -> None:
        r = await get_redis()
        key = Keys.k(Keys.FLOW, uid=user_id, name=name)
        await r.set(key, json.dumps(data, default=str), ex=ttl)

    @staticmethod
    async def get(user_id: int, name: str) -> Optional[dict]:
        r = await get_redis()
        raw = await r.get(Keys.k(Keys.FLOW, uid=user_id, name=name))
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    @staticmethod
    async def update(user_id: int, name: str, patch: dict, ttl: Optional[int] = None) -> Optional[dict]:
        current = await FlowState.get(user_id, name) or {}
        current.update(patch)
        await FlowState.set(user_id, name, current, ttl or FlowState.DEFAULT_TTL)
        return current

    @staticmethod
    async def clear(user_id: int, name: str) -> None:
        r = await get_redis()
        await r.delete(Keys.k(Keys.FLOW, uid=user_id, name=name))

    @staticmethod
    async def ttl(user_id: int, name: str) -> int:
        r = await get_redis()
        return int(await r.ttl(Keys.k(Keys.FLOW, uid=user_id, name=name)))


# ============================================================
# ❤️ HEARTBEAT / ONLINE
# ============================================================
class Presence:
    """Marca usuário online com TTL."""

    @staticmethod
    async def beat(user_id: int) -> None:
        r = await get_redis()
        await r.set(
            Keys.k(Keys.ONLINE, uid=user_id),
            int(time.time()),
            ex=settings.REDIS_HEARTBEAT_TTL,
        )
        await r.sadd(Keys.ONLINE_SET, str(user_id))

    @staticmethod
    async def is_online(user_id: int) -> bool:
        r = await get_redis()
        return bool(await r.exists(Keys.k(Keys.ONLINE, uid=user_id)))

    @staticmethod
    async def online_count() -> int:
        """Conta usuários online (reais — checando TTL)."""
        r = await get_redis()
        members = await r.smembers(Keys.ONLINE_SET)
        count = 0
        if not members:
            return 0
        for uid in members:
            if await r.exists(Keys.k(Keys.ONLINE, uid=uid)):
                count += 1
            else:
                await r.srem(Keys.ONLINE_SET, uid)
        return count

    @staticmethod
    async def clear(user_id: int) -> None:
        r = await get_redis()
        await r.delete(Keys.k(Keys.ONLINE, uid=user_id))
        await r.srem(Keys.ONLINE_SET, str(user_id))


# ============================================================
# 🚦 THROTTLE / RATE LIMIT
# ============================================================
class Throttle:
    """
    Impede spam de ações do usuário (ex.: 1 msg a cada X segundos).
    Retorna True se PODE executar.
    """

    @staticmethod
    async def allow(user_id: int, action: str, cooldown: Optional[float] = None) -> bool:
        cooldown = cooldown if cooldown is not None else settings.THROTTLE_RATE
        r = await get_redis()
        key = Keys.k(Keys.THROTTLE, uid=user_id, action=action)
        ok = await r.set(key, "1", ex=max(1, int(cooldown)), nx=True)
        return bool(ok)

    @staticmethod
    async def remaining(user_id: int, action: str) -> int:
        r = await get_redis()
        return int(await r.ttl(Keys.k(Keys.THROTTLE, uid=user_id, action=action)))


# ============================================================
# 📨 FILA LEVE (automações / notificações)
# ============================================================
class Queue:
    """Fila FIFO simples via Redis List."""

    @staticmethod
    async def push(name: str, payload: dict) -> None:
        r = await get_redis()
        await r.lpush(name, json.dumps(payload, default=str))

    @staticmethod
    async def pop(name: str) -> Optional[dict]:
        r = await get_redis()
        raw = await r.rpop(name)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    @staticmethod
    async def size(name: str) -> int:
        r = await get_redis()
        return int(await r.llen(name))


# ============================================================
# 📊 CONTADORES AO VIVO (dashboards)
# ============================================================
class Counters:
    @staticmethod
    async def incr(name: str, delta: int = 1) -> int:
        r = await get_redis()
        key = Keys.k(Keys.DASHBOARD, name=name)
        return int(await r.incrby(key, delta))

    @staticmethod
    async def get(name: str) -> int:
        r = await get_redis()
        val = await r.get(Keys.k(Keys.DASHBOARD, name=name))
        return int(val) if val else 0

    @staticmethod
    async def reset(name: str) -> None:
        r = await get_redis()
        await r.delete(Keys.k(Keys.DASHBOARD, name=name))


# ============================================================
# 🧪 UTIL: execução em lote
# ============================================================
async def pipeline() -> Pipeline:
    """Retorna um pipeline (uso: `async with await pipeline() as p:`)."""
    r = await get_redis()
    return r.pipeline(transaction=True)


# ============================================================
# 🧹 LIMPEZA
# ============================================================
async def clear_user_state(user_id: int) -> None:
    """Remove TODOS os estados de fluxo e views do usuário."""
    r = await get_redis()
    patterns = [
        Keys.k(Keys.FLOW, uid=user_id, name="*"),
        Keys.k(Keys.USER_VIEW, uid=user_id),
        Keys.k(Keys.USER_ACTION, uid=user_id, name="*"),
        Keys.k(Keys.THROTTLE, uid=user_id, action="*"),
    ]
    for pattern in patterns:
        async for key in r.scan_iter(match=pattern, count=100):
            await r.delete(key)


async def flush_prefix() -> int:
    """
    ATENÇÃO: apaga todas as chaves com o prefixo configurado.
    Use apenas em dev / testes.
    """
    r = await get_redis()
    deleted = 0
    async for key in r.scan_iter(match=f"{settings.REDIS_PREFIX}:*", count=500):
        await r.delete(key)
        deleted += 1
    return deleted


# ============================================================
# ⏱️ DELAYED JOB (auto-exclusão) — usado nas automações
# ============================================================
async def schedule_delayed(name: str, payload: dict, delay_sec: int) -> None:
    """
    Coloca um job na fila com timestamp de execução.
    O worker lê a fila e só executa quando `run_at <= now`.
    """
    payload = dict(payload)
    payload["_run_at"] = int(time.time()) + delay_sec
    payload["_name"] = name
    await Queue.push(Keys.QUEUE_AUTOMATION, payload)


async def pop_due_job() -> Optional[dict]:
    """Pega o próximo job pronto para executar (ou None)."""
    r = await get_redis()
    now = int(time.time())
    # lista ordenada por score (menor = mais antigo)
    key = f"{settings.REDIS_PREFIX}:jobs:delayed"
    due = await r.zrangebyscore(key, 0, now, start=0, num=1)
    if not due:
        return None
    raw = due[0]
    await r.zrem(key, raw)
    try:
        return json.loads(raw)
    except Exception:
        return None


async def push_delayed_job(payload: dict, delay_sec: int) -> None:
    """Empurra um job no Sorted Set com score = timestamp de execução."""
    r = await get_redis()
    run_at = int(time.time()) + delay_sec
    payload = dict(payload)
    payload["_run_at"] = run_at
    key = f"{settings.REDIS_PREFIX}:jobs:delayed"
    await r.zadd(key, {json.dumps(payload, default=str): run_at})


__all__ = [
    "Keys",
    "get_redis",
    "close_redis",
    "ping",
    # Cache
    "cache_set",
    "cache_get",
    "cache_delete",
    "cache_clear_namespace",
    # Lock
    "LockManager",
    "LockError",
    # Flow
    "FlowState",
    # Presence
    "Presence",
    # Throttle
    "Throttle",
    # Queue
    "Queue",
    "schedule_delayed",
    "pop_due_job",
    "push_delayed_job",
    # Counters
    "Counters",
    # Utils
    "pipeline",
    "clear_user_state",
    "flush_prefix",
]
