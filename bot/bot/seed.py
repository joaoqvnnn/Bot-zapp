"""
Larizinha Store — Seed Inicial
Popula o banco com dados padrão na PRIMEIRA execução.

Idempotente: se rodar novamente, NÃO duplica (checa antes de inserir).

Popula:
- Admins (main + whitelist do .env)
- Roles padrão (Admin Principal, Gerente, Financeiro, Suporte, Moderador)
- Categorias padrão (Streamings, Games, Gift Cards, Combos)
- Mensagens editáveis (todas as ~60 keys do DIM com fallbacks)
- Botões editáveis (menu principal, boas-vindas, catálogo, etc.)
- Configurações iniciais (nome, moeda, fuso, limites, etc.)
- Automações (toggles e delays padrão)

Uso:
    python -m bot.seed
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from sqlalchemy import func, select

from bot.config import settings
from bot.database import (
    Admin,
    Automation,
    ButtonTemplate,
    Category,
    MessageTemplate,
    Role,
    Setting,
    close_db,
    init_db,
    session_scope,
)
from bot.logger import logger, setup_logger


# ============================================================
# 🔑 CONSTANTES
# ============================================================
DEFAULT_ROLES: List[Dict[str, Any]] = [
    {
        "name": "Admin Principal",
        "description": "Acesso total a todos os módulos",
        "permissions": ["*"],
        "is_system": True,
    },
    {
        "name": "Gerente",
        "description": "Acesso sem exclusões críticas",
        "permissions": [
            "dashboard", "products", "categories", "sales", "users",
            "wallet", "payments", "gifts", "coupons", "promos", "channel",
            "affiliates", "messages", "buttons", "stats", "settings",
            "broadcast", "automations", "logs", "locks", "tickets",
            "games", "alerts", "tools",
        ],
        "is_system": True,
    },
    {
        "name": "Financeiro",
        "description": "Apenas saldo, pagamentos e saques",
        "permissions": [
            "dashboard", "wallet", "payments", "withdraws", "finance",
            "stats", "logs",
        ],
        "is_system": True,
    },
    {
        "name": "Suporte",
        "description": "Apenas atendimento e usuários",
        "permissions": [
            "dashboard", "users", "tickets", "alerts",
        ],
        "is_system": True,
    },
    {
        "name": "Moderador",
        "description": "Apenas moderação de usuários",
        "permissions": [
            "dashboard", "users", "blacklist", "security",
        ],
        "is_system": True,
    },
]


DEFAULT_CATEGORIES: List[Dict[str, Any]] = [
    {"name": "Streamings", "slug": "streamings", "emoji": "🎬", "order": 1},
    {"name": "Games", "slug": "games", "emoji": "🎮", "order": 2},
    {"name": "Gift Cards", "slug": "gift-cards", "emoji": "🎁", "order": 3},
    {"name": "Combos", "slug": "combos", "emoji": "📦", "order": 4},
    {"name": "Outros", "slug": "outros", "emoji": "🔹", "order": 99},
]


# ============================================================
# 📩 MENSAGENS PADRÃO (keys do DIM)
# ============================================================
DEFAULT_MESSAGES: List[Dict[str, str]] = [
    # ---------- Bloqueio de canal ----------
    {
        "key": "start_bloqueio",
        "screen": "start_bloqueio",
        "content": (
            "❗️ <b>Para utilizar nosso serviço é obrigatório</b>\n"
            "<b>que você entre no(s) canal(is) abaixo:</b>\n\n"
            "{{lista_canais}}\n\n"
            "<i>Depois de entrar, clique em ✅ Verificar abaixo.</i>"
        ),
    },
    # ---------- Boas-vindas ----------
    {
        "key": "boas_vindas",
        "screen": "boas_vindas",
        "content": (
            "🎬 <b>Bem-vindo(a) à {{nome_loja}}</b> ✨\n"
            "A sua central de streamings com entrega 100% automática.\n\n"
            "Pagou, recebeu. Sem filas, sem precisar falar com atendente, "
            "24 horas por dia! ⚡️\n\n"
            "🛡 <b>Segurança e Suporte:</b>\n"
            "Mais de {{qtd_clientes}} clientes já passaram por aqui.\n"
            "Participe da nossa comunidade e veja as referências.\n\n"
            "💠 <b>Seus Dados:</b>\n"
            "├ 👤 ID: <code>{{id}}</code>\n"
            "└ 💰 Saldo Atual: <b>R$ {{saldo}}</b>\n\n"
            "👇 <b>COMO COMEÇAR:</b>\n"
            "Clique no botão \"🛍 Comprar Produtos\" abaixo para ver nosso "
            "catálogo e escolher sua tela!"
        ),
    },
    # ---------- Catálogo ----------
    {
        "key": "catalogo",
        "screen": "catalogo",
        "content": (
            "📱 <b>{{nome_loja}} | Catálogo de Serviços</b>\n"
            "🔗🔗🔗🔗🔗🔗🔗🔗🔗🔗🔗\n\n"
            "💰| Saldo da Carteira: <b>R$ {{saldo}}</b>\n\n"
            "⬇️ Selecione um produto abaixo para ver os detalhes:\n\n"
            "{{lista}}"
        ),
    },
    # ---------- Detalhe do produto ----------
    {
        "key": "detalhe_produto",
        "screen": "detalhe_produto",
        "content": (
            "🔥 <b>OPORTUNIDADE EXCLUSIVA</b> 🔥\n"
            "🚀 <b>{{nome_produto}}</b>\n\n"
            "🟢 DISPONÍVEL AGORA\n"
            "├ 💵 Preço: <b>R$ {{preco}}</b>\n"
            "├ 💰 Seu Saldo: <b>R$ {{saldo}}</b>\n"
            "└ 📦 Estoque: <b>{{estoque}}</b> (ao vivo)\n\n"
            "📝 <b>Descrição:</b>\n{{descricao}}\n\n"
            "📊 <b>Estatísticas em tempo real:</b>\n"
            "⚡️ Já foram vendidas <b>{{vendidas}}</b> unidades!\n"
            "👀 <b>{{views}}</b> pessoas estão vendo isso agora.\n\n"
            "🛡 Garantia: <b>{{garantia}}</b> dias\n"
            "✅ Compra segura. Ao adquirir, concorda com os /termos"
        ),
    },
    # ---------- Saldo insuficiente ----------
    {
        "key": "saldo_insuficiente",
        "screen": "saldo_insuficiente",
        "content": (
            "❌ <b>Saldo insuficiente!</b>\n\n"
            "💰 Seu saldo: <b>R$ {{saldo}}</b>\n"
            "💵 Valor do produto: <b>R$ {{preco}}</b>\n"
            "📉 Faltam: <b>R$ {{falta}}</b>\n\n"
            "💡 Deseja gerar um PIX no valor de <b>R$ {{falta}}</b> "
            "para completar a compra?"
        ),
    },
    # ---------- Comprar mais de um ----------
    {
        "key": "comprar_mais",
        "screen": "comprar_mais",
        "content": (
            "🛒 <b>Comprar Mais de Um</b>\n\n"
            "📦 Produto: <b>{{nome_produto}}</b>\n"
            "💵 Preço unitário: <b>R$ {{preco}}</b>\n"
            "📊 Estoque disponível: <b>{{estoque}}</b>\n\n"
            "Quantos logins deseja comprar?\n\n"
            "💡 Digite /cancelar a qualquer momento para sair."
        ),
    },
    {
        "key": "comprar_mais_estoque_insuf",
        "screen": "comprar_mais_estoque_insuf",
        "content": (
            "❌ <b>Quantidade maior que o estoque disponível.</b>\n\n"
            "📦 Estoque atual: <b>{{estoque}}</b>"
        ),
    },
    # ---------- Compra OK ----------
    {
        "key": "compra_ok",
        "screen": "compra_ok",
        "content": (
            "✅ <b>Compra realizada com sucesso!</b>\n"
            "🛍 Produto: <b>{{nome}}</b>\n"
            "💰 Valor: <b>R$ {{preco}}</b>\n"
            "📆 Data: {{data}}\n"
            "⏰ Vencimento: {{vencimento}}\n\n"
            "📧 E-mail: <code>{{email}}</code>\n"
            "🔐 Senha: <code>{{senha}}</code>\n\n"
            "📃 {{nota_produto}}\n\n"
            "🔴 Status: DESATIVADO"
        ),
    },
    # ---------- Perfil ----------
    {
        "key": "perfil",
        "screen": "perfil",
        "content": (
            "👤 <b>Meu perfil</b>\n\n"
            "🔍 Veja aqui os detalhes da sua conta:\n\n"
            "- 👤 <b>Informações:</b>\n"
            "🆔 ID da Carteira: <code>{{id}}</code>\n"
            "💰 Saldo Atual: <b>R$ {{saldo}}</b>\n"
            "📲 Seu WhatsApp: {{whatsapp}}\n\n"
            "─── 📊 <b>Suas Movimentações:</b>\n"
            "ー 🛒 Compras Realizadas: <b>{{compras}}</b>\n"
            "ー 💰 Total Gasto Em Compras: <b>R$ {{gasto}}</b>\n"
            "ー 💠 PIX Inseridos: <b>R$ {{pix_in}}</b>\n"
            "ー 🎁 Gifts Resgatados: <b>R$ {{gifts}}</b>"
        ),
    },
    # ---------- Histórico ----------
    {
        "key": "historico_vazio",
        "screen": "historico",
        "content": (
            "📜 <b>HISTÓRICO DE COMPRAS</b>\n\n"
            "<i>Você ainda não possui compras registradas.</i>\n\n"
            "🛍 Aproveite para conferir nosso catálogo!"
        ),
    },
    {
        "key": "historico_cheio",
        "screen": "historico",
        "content": (
            "📜 <b>HISTÓRICO DE COMPRAS</b>\n"
            "Página {{pagina}}/{{total_paginas}}\n\n"
            "{{lista}}"
        ),
    },
    # ---------- Gift Card ----------
    {
        "key": "gift_prompt",
        "screen": "gift",
        "content": (
            "🎁 <b>Resgatar Gift Card</b>\n\n"
            "Envie o código do seu Gift Card abaixo.\n"
            "💡 Digite /cancelar para sair."
        ),
    },
    {
        "key": "gift_ok",
        "screen": "gift",
        "content": (
            "✅ <b>Gift Card resgatado!</b>\n\n"
            "💵 Valor creditado: <b>R$ {{valor}}</b>\n"
            "💰 Novo saldo: <b>R$ {{saldo}}</b>"
        ),
    },
    {
        "key": "gift_erro",
        "screen": "gift",
        "content": (
            "❌ <b>Gift Card inválido.</b>\n\n"
            "Verifique o código e tente novamente."
        ),
    },
    # ---------- Alterar dados ----------
    {
        "key": "alterar_dados",
        "screen": "alterar_dados",
        "content": (
            "✏️ <b>Alterar Dados</b>\n\n"
            "📱 WhatsApp atual: <b>{{whatsapp}}</b>\n\n"
            "Escolha o que deseja alterar:"
        ),
    },
    {
        "key": "whatsapp_prompt",
        "screen": "whatsapp",
        "content": (
            "📱 <b>Alterar WhatsApp</b>\n\n"
            "Envie seu número com DDD, <b>apenas números</b>.\n"
            "Exemplo: <code>11987654321</code>\n\n"
            "⚠️ A mensagem digitada será apagada automaticamente."
        ),
    },
    {
        "key": "whatsapp_ok",
        "screen": "whatsapp",
        "content": (
            "✅ <b>WhatsApp atualizado!</b>\n\n"
            "📱 Novo número: <b>{{whatsapp}}</b>"
        ),
    },
    {
        "key": "whatsapp_erro",
        "screen": "whatsapp",
        "content": (
            "❌ <b>Número inválido!</b>\n\n"
            "Envie apenas números com DDD (10 a 13 dígitos)."
        ),
    },
    # ---------- Recarregar ----------
    {
        "key": "recarregar",
        "screen": "recarregar",
        "content": (
            "🆔| ID da Carteira: <code>{{id}}</code>\n"
            "💰| Saldo Disponível: <b>R$ {{saldo}}</b>\n\n"
            "📍 Opte por 💠 PIX Rápido para que seu saldo seja creditado "
            "imediatamente.\n\n"
            "💡 Selecione uma opção para recarregar:"
        ),
    },
    {
        "key": "valor_prompt",
        "screen": "recarregar",
        "content": (
            "ℹ️ <b>Informe o valor que deseja recarregar:</b>\n\n"
            "🔻 Recarga mínima: <b>R$ {{min}}</b>\n\n"
            "⚠️ Por favor, envie o valor que deseja recarregar agora.\n"
            "Ao realizar um depósito você declara ter lido e estar de "
            "acordo com nossos /termos\n\n"
            "🎁 Bônus de recarga: <b>{{bonus}}%</b>\n"
            "❗️ Recarga mínima para ganhar o bônus: <b>R$ {{min_bonus}}</b>"
        ),
    },
    {
        "key": "valor_invalido",
        "screen": "recarregar",
        "content": "❌ <b>Valor inválido.</b>\n\nEnvie um número maior que zero.",
    },
    {
        "key": "valor_min",
        "screen": "recarregar",
        "content": (
            "❌ <b>Valor abaixo do mínimo.</b>\n\n"
            "🔻 Recarga mínima: <b>R$ {{min}}</b>"
        ),
    },
    {
        "key": "valor_max",
        "screen": "recarregar",
        "content": (
            "❌ <b>Valor acima do máximo.</b>\n\n"
            "🔺 Recarga máxima: <b>R$ {{max}}</b>"
        ),
    },
    # ---------- PIX ----------
    {
        "key": "pix_gerando",
        "screen": "pix",
        "content": "⏳ <b>Gerando pagamento...</b>",
    },
    {
        "key": "pix_qr",
        "screen": "pix",
        "content": (
            "💰 <b>Comprar Saldo com PIX Automático</b>\n\n"
            "⏱️ Expira em: <b>{{min}} Minutos</b>\n"
            "💵 Valor: <b>R$ {{valor}}</b>\n"
            "✨ ID da Recarga: <code>{{txid}}</code>\n\n"
            "📃 Atenção: Este código é válido para apenas um único pagamento.\n\n"
            "💎 <b>PIX Copia e Cola:</b>\n"
            "<code>{{pix_copia_cola}}</code>\n\n"
            "💡 Dica: Clique no código acima para copiar.\n\n"
            "📊 <b>Dados:</b>\n"
            "— 💰 Saldo Atual: R$ {{saldo}}\n"
            "— 🎁 Bônus a receber: R$ {{bonus}}\n"
            "— 💸 Saldo após o pagamento: R$ {{saldo_final}}\n\n"
            "🇧🇷 Após o pagamento, seu saldo será liberado instantaneamente."
        ),
    },
    {
        "key": "pix_ok",
        "screen": "pix",
        "content": (
            "✅ <b>Pagamento confirmado!</b>\n\n"
            "💵 Valor creditado: <b>R$ {{valor}}</b>\n"
            "🎁 Bônus: <b>R$ {{bonus}}</b>\n"
            "💰 Novo saldo: <b>R$ {{saldo}}</b>"
        ),
    },
    {
        "key": "pix_expirado",
        "screen": "pix",
        "content": (
            "⌛️ <b>PAGAMENTO PIX EXPIRADO</b>\n\n"
            "⚠️ O tempo limite para realizar este pagamento foi excedido.\n\n"
            "🆔 Referência: <code>{{txid}}</code>\n"
            "💸 Valor Solicitado: <b>R$ {{valor}}</b>"
        ),
    },
    {
        "key": "pix_naopago",
        "screen": "pix",
        "content": (
            "⏰ <b>Seu pagamento expirou!</b>\n"
            "Vimos que você gerou um PIX de <b>R$ {{valor}}</b> mas não "
            "conseguiu finalizar.\n\n"
            "🤔 Aconteceu algum problema?\n\n"
            "Se precisar, você pode gerar um novo pagamento a qualquer "
            "momento. Estamos aqui para ajudar!\n\n"
            "💡 Dica: O PIX é instantâneo e a liberação é automática!"
        ),
    },
    {
        "key": "carrinho",
        "screen": "carrinho",
        "content": (
            "👋 <b>Ei, você esqueceu algo!</b>\n"
            "Notamos que você estava olhando o produto <b>{{nome_produto}}</b> "
            "mas não finalizou a compra.\n\n"
            "💰 Valor: <b>R$ {{preco}}</b>\n\n"
            "🎁 Que tal finalizar agora?\n"
            "Seu produto está esperando por você!\n\n"
            "Se tiver alguma dúvida, estamos aqui para ajudar!"
        ),
    },
    {
        "key": "promo_nova",
        "screen": "promo",
        "content": (
            "😳 <b>O que você procurava chegou!</b>\n\n"
            "🚀 <b>{{nome_produto}}</b>\n"
            "🔥 <i>{{subtitulo}}</i>\n\n"
            "💰| Apenas <b>R$ {{preco}}</b>\n\n"
            "😨 Pouquíssimas unidades disponível!\n"
            "⌛️ Promoção válida enquanto durar o estoque!"
        ),
    },
    # ---------- Afiliados ----------
    {
        "key": "afiliados_inativo",
        "screen": "afiliados",
        "content": (
            "💰 <b>PROGRAMA DE AFILIADOS</b>\n\n"
            "⚙️ Status: <b>Inativo</b>\n"
            "🧲 Sua comissão: <b>{{comissao}}%</b>\n\n"
            "Ative o programa e comece a ganhar comissões a cada compra "
            "ou recarga dos seus indicados.\n\n"
            "🔗 Seu link (após ativar): <code>{{link}}</code>"
        ),
    },
    {
        "key": "afiliados_ativo",
        "screen": "afiliados",
        "content": (
            "💰 <b>PROGRAMA DE AFILIADOS</b>\n\n"
            "⚙️ Status: <b>Ativo</b>\n"
            "🧲 Sua comissão: <b>{{comissao}}%</b> "
            "(de todas recargas do indicado)\n\n"
            "👥 Indicações: <b>{{ind}}</b>\n"
            "🪙 Total ganho: <b>R$ {{ganho}}</b>\n"
            "📊 Média: <b>R$ {{media}}</b>\n"
            "💰 Saque mínimo: <b>R$ {{min_saque}}</b>\n\n"
            "🔥 Saldo de comissões: <b>R$ {{comissao_saldo}}</b>\n\n"
            "🌱 Nível: {{nivel}}\n"
            "🎯 Próxima meta: {{meta}} ({{restantes}} restantes)\n\n"
            "ℹ️ <b>INFO:</b> Seus indicados continuarão gerando comissão "
            "para sempre. A comissão pode ser alterada a qualquer momento.\n\n"
            "🔗 Seu link: <code>{{link}}</code>"
        ),
    },
    # ---------- Saque ----------
    {
        "key": "sacar_prompt",
        "screen": "sacar",
        "content": (
            "Olá, <b>{{nome}}</b>!\n"
            "💰 Saldo disponível: <b>R$ {{saldo}}</b>\n\n"
            "💸 Para sacar, cadastre ou escolha sua chave PIX:"
        ),
    },
    {
        "key": "chave_prompt",
        "screen": "sacar",
        "content": (
            "Envie sua chave PIX:\n\n"
            "⚠️ A mensagem digitada será apagada automaticamente por "
            "segurança."
        ),
    },
    {
        "key": "chave_invalida",
        "screen": "sacar",
        "content": (
            "❌ <b>Chave inválida.</b>\n\n"
            "Envie novamente no formato correto."
        ),
    },
    {
        "key": "sacar_confirmacao",
        "screen": "sacar",
        "content": (
            "💸 <b>CONFIRMAÇÃO DE SAQUE</b>\n\n"
            "🪪 Chave PIX: <code>{{chave_informada}}</code>\n"
            "💰 Valor a sacar: <b>R$ {{saldo}}</b>\n"
            "🎫 ID da Operação: <code>{{txid}}</code>\n\n"
            "⚠️ Confira os dados acima antes de prosseguir.\n"
            "Se a chave estiver errada, clique em \"✏️ Editar\" para corrigir."
        ),
    },
    {
        "key": "senha_prompt",
        "screen": "sacar",
        "content": (
            "🔐 Digite sua senha de saque:\n\n"
            "⚠️ A mensagem digitada será apagada automaticamente por "
            "segurança."
        ),
    },
    {
        "key": "senha_errada",
        "screen": "sacar",
        "content": "❌ <b>Senha incorreta.</b> Tente novamente.",
    },
    {
        "key": "processando",
        "screen": "sacar",
        "content": "⏳ <b>Processando pagamento...</b>",
    },
    {
        "key": "saque_ok",
        "screen": "sacar",
        "content": (
            "✅ <b>Pagamento efetuado com sucesso!</b>\n\n"
            "💵 Valor: <b>R$ {{valor}}</b>\n"
            "🪪 Chave PIX: <code>{{chave}}</code>\n"
            "🎫 ID: <code>{{txid}}</code>"
        ),
    },
    # ---------- Ranking ----------
    {
        "key": "ranking",
        "screen": "ranking",
        "content": (
            "🏆 <b>Ranking dos serviços mais vendidos</b>\n\n"
            "{{lista}}"
        ),
    },
    # ---------- Suporte / Sobre ----------
    {
        "key": "atendimento",
        "screen": "atendimento",
        "content": (
            "✉️ <b>ATENDIMENTO</b>\n\n"
            "Clique no botão abaixo para falar com nosso suporte.\n"
            "Uma conversa será aberta no seu privado com uma mensagem pronta."
        ),
    },
    {
        "key": "sobre_bot",
        "screen": "sobre_bot",
        "content": (
            "🤖 <b>SOBRE O BOT</b>\n"
            "Versão: <b>{{versao}}</b>\n"
            "Desenvolvido para <b>{{nome_loja}}</b>\n"
            "📞 Contato: {{whatsapp_suporte}}"
        ),
    },
    # ---------- Pesquisa ----------
    {
        "key": "pesquisar",
        "screen": "pesquisar",
        "content": (
            "🔍 <b>Como procurar um serviço?</b>\n\n"
            "Digite: <code>procurar &lt;nome do serviço&gt;</code>\n\n"
            "@{{username_bot}}\n"
            "<code>procurar</code>"
        ),
    },
    {
        "key": "resultado_busca",
        "screen": "resultado_busca",
        "content": (
            "🔍 Resultado para \"{{busca}}\"\n\n"
            "🎯 <b>{{nome_produto}}</b>\n"
            "💲 Valor: <b>R$ {{preco}}</b>\n"
            "📝 {{descricao_curta}}\n\n"
            "Para comprar, clique no botão \"💳 Comprar\" abaixo."
        ),
    },
    {
        "key": "pesquisa_sem_resultado",
        "screen": "pesquisar",
        "content": (
            "🔍 <b>Nenhum resultado para</b> \"{{busca}}\"\n\n"
            "Tente outro termo ou veja o catálogo completo."
        ),
    },
    # ---------- Loja WebApp ----------
    {
        "key": "loja_webapp",
        "screen": "loja_webapp",
        "content": (
            "🛒 <b>LOJA {{nome_loja}}</b>\n\n"
            "👤 <b>{{nome}}</b>\n"
            "💰 Saldo atual: <b>R$ {{saldo}}</b>\n\n"
            "Clique no botão abaixo para abrir a loja e adicionar saldo "
            "diretamente pelo mini-app."
        ),
    },
    # ---------- Canal (auto-post) ----------
    {
        "key": "canal_compra",
        "screen": "canal_compra",
        "content": (
            "💎 <b>NOVO ACESSO LIBERADO</b>\n\n"
            "👤 Usuário: {{nome}}\n"
            "📦 Validade: {{validade}}\n"
            "💵 Status: {{status}}\n"
            "🕐 Data: {{data}}\n\n"
            "🚀 Acesso liberado automaticamente"
        ),
    },
    {
        "key": "canal_estoque",
        "screen": "canal_estoque",
        "content": (
            "🎁 <b>NOVO ESTOQUE ADICIONADO!</b>\n\n"
            "📦 {{nome_produto}}\n"
            "📊 Quantidade: {{qtd}}\n\n"
            "🏃‍♂️ Corra e garanta o seu!"
        ),
    },
    {
        "key": "canal_promo",
        "screen": "canal_promo",
        "content": (
            "😳 <b>O que você procurava chegou!</b>\n\n"
            "🚀 <b>{{nome_produto}}</b>\n"
            "🔥 <i>{{subtitulo}}</i>\n\n"
            "💰| Apenas <b>R$ {{preco}}</b>\n\n"
            "😨 Pouquíssimas unidades disponível!\n"
            "⌛️ Promoção válida enquanto durar o estoque!"
        ),
    },
    # ---------- Disparo ----------
    {
        "key": "disparo",
        "screen": "disparo",
        "content": "📨 {{conteudo}}",
    },
    # ---------- Comandos ----------
    {
        "key": "cmd_id",
        "screen": "cmd_id",
        "content": (
            "🆔 <b>Seu ID</b>\n\n"
            "<code>{{id}}</code>\n\n"
            "Use este ID para receber recargas ou suporte."
        ),
    },
    {
        "key": "cmd_saldo",
        "screen": "cmd_saldo",
        "content": (
            "╭───────────────────╮\n"
            "💰 Carteira id: <code>{{id}}</code>\n"
            "💸 Saldo: <b>R$ {{saldo}}</b>\n"
            "╰───────────────────╯"
        ),
    },
    {
        "key": "cmd_termos",
        "screen": "cmd_termos",
        "content": (
            "📜 <b>TERMOS DE USO</b>\n\n"
            "Ao utilizar nossos serviços, você declara estar de acordo:\n\n"
            "• Uso pessoal e intransferível\n"
            "• Não compartilhar credenciais\n"
            "• Não solicitar reembolso após entrega\n"
            "• Respeitar a garantia informada no produto\n\n"
            "© {{nome_loja}} · v{{versao}}"
        ),
    },
    {
        "key": "cmd_pix_formato",
        "screen": "cmd_pix",
        "content": (
            "⚠️ <b>Formato incorreto</b>\n\n"
            "Envie: <code>/pix 10</code>\n"
            "ou: <code>/pix 5.25</code>"
        ),
    },
    {
        "key": "cmd_alertas",
        "screen": "cmd_alertas",
        "content": (
            "⚠️ <b>Sistema de alertas</b>\n\n"
            "Seja notificado quando seu serviço favorito for abastecido 🤩\n\n"
            "🎯 Basta selecionar abaixo os serviços que você deseja ser "
            "notificado, e eu lhe avisarei sempre que for abastecido.\n\n"
            "✅ Nossos produtos são de grandes demandas e acabam rápido, "
            "é importante que você seja notificado para aproveitar antes "
            "que acabe!\n\n"
            "Lista de serviços que você pode ser notificado ⤵️"
        ),
    },
    {
        "key": "cmd_afiliados",
        "screen": "cmd_afiliados",
        "content": "💰 <b>PROGRAMA DE AFILIADOS</b>\n\nUse os botões abaixo.",
    },
    {
        "key": "cmd_ranking",
        "screen": "cmd_ranking",
        "content": "🏆 <b>Ranking</b>\n\n{{lista}}",
    },
    {
        "key": "cmd_cancelar",
        "screen": "cmd_cancelar",
        "content": "✅ <b>Operação cancelada.</b>\n\nVoltando ao início...",
    },
    # ---------- Admin ----------
    {
        "key": "admin_menu",
        "screen": "admin_menu",
        "content": (
            "╔══════════════════════════════════╗\n"
            "║    🛡️ PAINEL ADMINISTRATIVO       ║\n"
            "║    {{nome_loja}} · v{{versao}}    ║\n"
            "╚══════════════════════════════════╝\n\n"
            "👤 Admin: <b>{{nome_admin}}</b>\n"
            "🆔 ID: <code>{{id_admin}}</code>\n"
            "🕐 Último acesso: {{ultimo_acesso}}"
        ),
    },
    {
        "key": "admin_negado",
        "screen": "admin",
        "content": (
            "⛔ <b>Acesso negado.</b>\n\n"
            "Você não tem permissão para acessar o painel."
        ),
    },
    {
        "key": "admin_sair",
        "screen": "admin",
        "content": (
            "👋 <b>Você saiu do Painel Administrativo.</b>\n\n"
            "Para voltar, mande <code>/admin</code> no chat privado."
        ),
    },
]


# ============================================================
# 🔘 BOTÕES PADRÃO
# ============================================================
DEFAULT_BUTTONS: List[Dict[str, Any]] = [
    # ---------- Boas-vindas ----------
    {
        "screen": "boas_vindas",
        "key": "btn_comprar",
        "text": "Comprar Produtos",
        "emoji": "🛍️",
        "type": "callback",
        "callback_data": "catalog:open",
        "row": 1, "position": 1,
    },
    {
        "screen": "boas_vindas",
        "key": "btn_loja",
        "text": "Abrir Loja",
        "emoji": "🛒",
        "type": "callback",
        "callback_data": "shop:open",
        "row": 1, "position": 2,
    },
    {
        "screen": "boas_vindas",
        "key": "btn_perfil",
        "text": "Meu Perfil",
        "emoji": "👤",
        "type": "callback",
        "callback_data": "profile:open",
        "row": 2, "position": 1,
    },
    {
        "screen": "boas_vindas",
        "key": "btn_recarregar",
        "text": "Recarregar Saldo",
        "emoji": "💠",
        "type": "callback",
        "callback_data": "recharge:open",
        "row": 2, "position": 2,
    },
    {
        "screen": "boas_vindas",
        "key": "btn_afiliados",
        "text": "Afiliados",
        "emoji": "👥",
        "type": "callback",
        "callback_data": "aff:open",
        "row": 3, "position": 1,
    },
    {
        "screen": "boas_vindas",
        "key": "btn_ranking",
        "text": "Top Compradores",
        "emoji": "🏆",
        "type": "callback",
        "callback_data": "ranking:open",
        "row": 3, "position": 2,
    },
    {
        "screen": "boas_vindas",
        "key": "btn_atendimento",
        "text": "Atendimento",
        "emoji": "✉️",
        "type": "callback",
        "callback_data": "support:open",
        "row": 4, "position": 1,
    },
    {
        "screen": "boas_vindas",
        "key": "btn_sobre",
        "text": "Sobre o Bot",
        "emoji": "🤖",
        "type": "callback",
        "callback_data": "support:about",
        "row": 4, "position": 2,
    },
    {
        "screen": "boas_vindas",
        "key": "btn_pesquisar",
        "text": "Pesquisar Serviços",
        "emoji": "🔍",
        "type": "callback",
        "callback_data": "search:open",
        "row": 5, "position": 1,
    },
    # ---------- Perfil ----------
    {
        "screen": "perfil",
        "key": "btn_hist",
        "text": "Histórico de Compras",
        "emoji": "📜",
        "type": "callback",
        "callback_data": "profile:hist:page:1",
        "row": 1, "position": 1,
    },
    {
        "screen": "perfil",
        "key": "btn_gift",
        "text": "Resgatar Gift Card",
        "emoji": "🎁",
        "type": "callback",
        "callback_data": "profile:gift",
        "row": 2, "position": 1,
    },
    {
        "screen": "perfil",
        "key": "btn_edit",
        "text": "Alterar Dados",
        "emoji": "✏️",
        "type": "callback",
        "callback_data": "profile:edit",
        "row": 2, "position": 2,
    },
    {
        "screen": "perfil",
        "key": "btn_voltar",
        "text": "Voltar",
        "emoji": "🔙",
        "type": "callback",
        "callback_data": "catalog:open",
        "row": 3, "position": 1,
    },
    # ---------- Recarregar ----------
    {
        "screen": "recarregar",
        "key": "btn_pix_rapido",
        "text": "PIX Rápido",
        "emoji": "💠",
        "type": "callback",
        "callback_data": "recharge:fast",
        "row": 1, "position": 1,
    },
    {
        "screen": "recarregar",
        "key": "btn_voltar",
        "text": "Voltar",
        "emoji": "🔙",
        "type": "callback",
        "callback_data": "catalog:open",
        "row": 2, "position": 1,
    },
    # ---------- PIX ----------
    {
        "screen": "pix_qr",
        "key": "btn_check",
        "text": "Aguardando Pagamento",
        "emoji": "🔄",
        "type": "callback",
        "callback_data": "recharge:check",
        "row": 1, "position": 1,
    },
    {
        "screen": "pix_qr",
        "key": "btn_copy",
        "text": "Copiar PIX",
        "emoji": "📋",
        "type": "copy",
        "callback_data": "recharge:copy",
        "row": 1, "position": 2,
    },
    # ---------- PIX expirado ----------
    {
        "screen": "pix_expirado",
        "key": "btn_novo_pix",
        "text": "Gerar Novo PIX",
        "emoji": "🔄",
        "type": "callback",
        "callback_data": "recharge:new",
        "row": 1, "position": 1,
    },
    {
        "screen": "pix_expirado",
        "key": "btn_voltar",
        "text": "Voltar",
        "emoji": "🔙",
        "type": "callback",
        "callback_data": "catalog:open",
        "row": 2, "position": 1,
    },
    # ---------- Sacar ----------
    {
        "screen": "sacar",
        "key": "btn_email",
        "text": "E-mail",
        "emoji": "📧",
        "type": "callback",
        "callback_data": "withdraw:key:email",
        "row": 1, "position": 1,
    },
    {
        "screen": "sacar",
        "key": "btn_cpf",
        "text": "CPF",
        "emoji": "🪪",
        "type": "callback",
        "callback_data": "withdraw:key:cpf",
        "row": 1, "position": 2,
    },
    {
        "screen": "sacar",
        "key": "btn_gmail",
        "text": "Gmail",
        "emoji": "📧",
        "type": "callback",
        "callback_data": "withdraw:key:gmail",
        "row": 2, "position": 1,
    },
    {
        "screen": "sacar",
        "key": "btn_random",
        "text": "Chave Aleatória",
        "emoji": "🎲",
        "type": "callback",
        "callback_data": "withdraw:key:random",
        "row": 2, "position": 2,
    },
    {
        "screen": "sacar",
        "key": "btn_voltar",
        "text": "Voltar",
        "emoji": "🔙",
        "type": "callback",
        "callback_data": "profile:open",
        "row": 3, "position": 1,
    },
    # ---------- Confirmação de saque ----------
    {
        "screen": "sacar_confirmacao",
        "key": "btn_confirmar",
        "text": "Confirmar",
        "emoji": "✅",
        "type": "callback",
        "callback_data": "withdraw:confirm",
        "row": 1, "position": 1,
    },
    {
        "screen": "sacar_confirmacao",
        "key": "btn_editar",
        "text": "Editar",
        "emoji": "✏️",
        "type": "callback",
        "callback_data": "withdraw:edit",
        "row": 1, "position": 2,
    },
    {
        "screen": "sacar_confirmacao",
        "key": "btn_cancelar",
        "text": "Cancelar",
        "emoji": "❌",
        "type": "callback",
        "callback_data": "withdraw:cancel",
        "row": 1, "position": 3,
    },
    # ---------- Afiliados inativo ----------
    {
        "screen": "afiliados_inativo",
        "key": "btn_ativar",
        "text": "Ativar",
        "emoji": "✅",
        "type": "callback",
        "callback_data": "aff:activate",
        "row": 1, "position": 1,
    },
    {
        "screen": "afiliados_inativo",
        "key": "btn_voltar",
        "text": "Voltar",
        "emoji": "🔙",
        "type": "callback",
        "callback_data": "profile:open",
        "row": 2, "position": 1,
    },
    # ---------- Afiliados ativo ----------
    {
        "screen": "afiliados_ativo",
        "key": "btn_hist",
        "text": "Histórico de Saques",
        "emoji": "📊",
        "type": "callback",
        "callback_data": "aff:hist:page:1",
        "row": 1, "position": 1,
    },
    {
        "screen": "afiliados_ativo",
        "key": "btn_sacar",
        "text": "Sacar",
        "emoji": "💸",
        "type": "callback",
        "callback_data": "aff:withdraw",
        "row": 2, "position": 1,
    },
    {
        "screen": "afiliados_ativo",
        "key": "btn_senha",
        "text": "Cadastrar Senha de Saque",
        "emoji": "🔐",
        "type": "callback",
        "callback_data": "aff:password",
        "row": 2, "position": 2,
    },
    {
        "screen": "afiliados_ativo",
        "key": "btn_voltar",
        "text": "Voltar",
        "emoji": "🔙",
        "type": "callback",
        "callback_data": "profile:open",
        "row": 3, "position": 1,
    },
    # ---------- Atendimento ----------
    {
        "screen": "atendimento",
        "key": "btn_abrir",
        "text": "Abrir Atendimento",
        "emoji": "💬",
        "type": "callback",
        "callback_data": "support:abrir",
        "row": 1, "position": 1,
    },
    {
        "screen": "atendimento",
        "key": "btn_voltar",
        "text": "Voltar",
        "emoji": "🔙",
        "type": "callback",
        "callback_data": "catalog:open",
        "row": 2, "position": 1,
    },
    # ---------- Sobre o bot ----------
    {
        "screen": "sobre_bot",
        "key": "btn_voltar",
        "text": "Voltar",
        "emoji": "🔙",
        "type": "callback",
        "callback_data": "catalog:open",
        "row": 1, "position": 1,
    },
    # ---------- Ranking ----------
    {
        "screen": "ranking",
        "key": "btn_servicos",
        "text": "Serviços",
        "emoji": "☑️",
        "type": "callback",
        "callback_data": "ranking:cat:servicos",
        "row": 1, "position": 1,
    },
    {
        "screen": "ranking",
        "key": "btn_recargas",
        "text": "Recargas",
        "emoji": "☑️",
        "type": "callback",
        "callback_data": "ranking:cat:recargas",
        "row": 1, "position": 2,
    },
    {
        "screen": "ranking",
        "key": "btn_compras",
        "text": "Compras",
        "emoji": "☑️",
        "type": "callback",
        "callback_data": "ranking:cat:compras",
        "row": 2, "position": 1,
    },
    {
        "screen": "ranking",
        "key": "btn_gifts",
        "text": "Gift Card",
        "emoji": "☑️",
        "type": "callback",
        "callback_data": "ranking:cat:gifts",
        "row": 2, "position": 2,
    },
    {
        "screen": "ranking",
        "key": "btn_saldo",
        "text": "Saldo",
        "emoji": "☑️",
        "type": "callback",
        "callback_data": "ranking:cat:saldo",
        "row": 3, "position": 1,
    },
    {
        "screen": "ranking",
        "key": "btn_voltar",
        "text": "Voltar",
        "emoji": "🔙",
        "type": "callback",
        "callback_data": "catalog:open",
        "row": 4, "position": 1,
    },
    # ---------- Detalhe do produto ----------
    {
        "screen": "detalhe_produto",
        "key": "btn_comprar",
        "text": "Comprar",
        "emoji": "🛒",
        "type": "callback",
        "callback_data": "catalog:buy:0",
        "row": 1, "position": 1,
    },
    {
        "screen": "detalhe_produto",
        "key": "btn_comprar_multi",
        "text": "Comprar Mais de Um",
        "emoji": "🛒",
        "type": "callback",
        "callback_data": "catalog:buymulti:0",
        "row": 2, "position": 1,
    },
    {
        "screen": "detalhe_produto",
        "key": "btn_voltar",
        "text": "Voltar",
        "emoji": "🔙",
        "type": "callback",
        "callback_data": "catalog:open",
        "row": 3, "position": 1,
    },
    # ---------- Compra OK ----------
    {
        "screen": "compra_ok",
        "key": "btn_ativar",
        "text": "Ativar",
        "emoji": "🟢",
        "type": "callback",
        "callback_data": "activate:0",
        "row": 1, "position": 1,
    },
    {
        "screen": "compra_ok",
        "key": "btn_voltar",
        "text": "Voltar",
        "emoji": "🔙",
        "type": "callback",
        "callback_data": "catalog:open",
        "row": 2, "position": 1,
    },
    # ---------- Canal (auto-post) ----------
    {
        "screen": "canal_compra",
        "key": "btn_acessar",
        "text": "Acessar o Bot",
        "emoji": "🚀",
        "type": "url",
        "url": "https://t.me/{{bot_username}}",
        "row": 1, "position": 1,
    },
    {
        "screen": "canal_estoque",
        "key": "btn_comprar",
        "text": "Comprar Agora",
        "emoji": "🛒",
        "type": "url",
        "url": "https://t.me/{{bot_username}}",
        "row": 1, "position": 1,
    },
    {
        "screen": "canal_promo",
        "key": "btn_aproveitar",
        "text": "Aproveitar Agora",
        "emoji": "🎯",
        "type": "url",
        "url": "https://t.me/{{bot_username}}",
        "row": 1, "position": 1,
    },
]


# ============================================================
# ⚙️ CONFIGURAÇÕES INICIAIS
# ============================================================
DEFAULT_SETTINGS: List[Dict[str, Any]] = [
    # ----- Identidade -----
    {"key": "store_name", "value": settings.APP_NAME, "type": "string", "category": "settings"},
    {"key": "store_description", "value": "Central de streamings com entrega automática", "type": "string", "category": "settings"},
    # ----- Localização -----
    {"key": "currency", "value": "BRL", "type": "string", "category": "settings"},
    {"key": "timezone", "value": settings.TIMEZONE, "type": "string", "category": "settings"},
    {"key": "language", "value": "pt-BR", "type": "string", "category": "settings"},
    {"key": "value_format", "value": "R$ {valor}", "type": "string", "category": "settings"},
    # ----- Financeiro -----
    {"key": "initial_balance", "value": str(settings.INITIAL_BALANCE), "type": "money", "category": "settings"},
    {"key": "min_purchase", "value": "0.00", "type": "money", "category": "settings"},
    {"key": "global_fee_percent", "value": "0", "type": "number", "category": "settings"},
    # ----- Depósitos -----
    {"key": "min_deposit", "value": str(settings.MIN_DEPOSIT), "type": "money", "category": "payments"},
    {"key": "max_deposit", "value": str(settings.MAX_DEPOSIT), "type": "money", "category": "payments"},
    {"key": "pix_enabled", "value": "true", "type": "bool", "category": "payments"},
    {"key": "pix_fee_percent", "value": "0", "type": "number", "category": "payments"},
    {"key": "pix_expiration_minutes", "value": str(settings.MP_PIX_EXPIRATION_MINUTES), "type": "int", "category": "payments"},
    {"key": "bonus_percent", "value": str(settings.DEPOSIT_BONUS_PERCENT), "type": "number", "category": "payments"},
    {"key": "bonus_min", "value": str(settings.DEPOSIT_BONUS_MIN), "type": "money", "category": "payments"},
    {"key": "auto_refund", "value": "false", "type": "bool", "category": "payments"},
    # ----- Saques -----
    {"key": "min_withdraw", "value": str(settings.MIN_WITHDRAW), "type": "money", "category": "withdraws"},
    {"key": "max_withdraw", "value": str(settings.MAX_WITHDRAW), "type": "money", "category": "withdraws"},
    {"key": "withdraw_fee_percent", "value": str(settings.WITHDRAW_FEE_PERCENT), "type": "number", "category": "withdraws"},
    {"key": "withdraw_daily_limit", "value": str(settings.WITHDRAW_DAILY_LIMIT), "type": "int", "category": "withdraws"},
    {"key": "withdraw_auto_approve", "value": str(settings.WITHDRAW_AUTO_APPROVE).lower(), "type": "bool", "category": "withdraws"},
    {"key": "withdraw_processing_days", "value": str(settings.WITHDRAW_PROCESSING_DAYS), "type": "int", "category": "withdraws"},
    # ----- Afiliados -----
    {"key": "affiliate_commission_percent", "value": str(settings.AFFILIATE_COMMISSION_PERCENT), "type": "number", "category": "affiliates"},
    {"key": "affiliate_commission_deposit_percent", "value": str(settings.AFFILIATE_COMMISSION_DEPOSIT_PERCENT), "type": "number", "category": "affiliates"},
    {"key": "affiliate_min_withdraw", "value": str(settings.AFFILIATE_MIN_WITHDRAW), "type": "money", "category": "affiliates"},
    {"key": "affiliate_enabled", "value": "true", "type": "bool", "category": "affiliates"},
    # ----- Segurança -----
    {"key": "antifraud_enabled", "value": str(settings.ANTIFRAUD_ENABLED).lower(), "type": "bool", "category": "security"},
    {"key": "antifraud_max_accounts_per_ip", "value": str(settings.ANTIFRAUD_MAX_ACCOUNTS_PER_IP), "type": "int", "category": "security"},
    {"key": "antifraud_suspicious_amount", "value": str(settings.ANTIFRAUD_SUSPICIOUS_AMOUNT), "type": "money", "category": "security"},
    {"key": "throttle_rate", "value": str(settings.THROTTLE_RATE), "type": "number", "category": "security"},
    {"key": "temp_block_minutes", "value": str(settings.TEMP_BLOCK_MINUTES), "type": "int", "category": "security"},
    # ----- Backup -----
    {"key": "backup_auto_enabled", "value": "true", "type": "bool", "category": "backup"},
    {"key": "backup_interval_hours", "value": str(settings.AUTO_BACKUP_INTERVAL_HOURS), "type": "int", "category": "backup"},
    {"key": "backup_retention_days", "value": str(settings.BACKUP_RETENTION_DAYS), "type": "int", "category": "backup"},
    {"key": "backup_max_keep", "value": "30", "type": "int", "category": "backup"},
    # ----- Logs -----
    {"key": "logs_retention_days", "value": "90", "type": "int", "category": "logs"},
    # ----- Rede/API -----
    {"key": "api_timeout", "value": "20", "type": "int", "category": "api"},
    {"key": "api_max_retries", "value": "3", "type": "int", "category": "api"},
]


# ============================================================
# 🧠 AUTOMAÇÕES PADRÃO
# ============================================================
DEFAULT_AUTOMATIONS: List[Dict[str, Any]] = [
    {"key": "auto_pix_expired", "name": "PIX Expirado", "enabled": settings.AUTO_PIX_EXPIRED,
     "delay_min": settings.AUTO_PIX_EXPIRED_DELAY},
    {"key": "auto_pix_unpaid", "name": "PIX Não Pago", "enabled": settings.AUTO_PIX_UNPAID,
     "delay_min": settings.AUTO_PIX_UNPAID_DELAY},
    {"key": "auto_cart_abandoned", "name": "Carrinho Abandonado", "enabled": settings.AUTO_CART_ABANDONED,
     "delay_min": settings.AUTO_CART_ABANDONED_DELAY},
    {"key": "auto_new_promo", "name": "Promoção Nova", "enabled": settings.AUTO_NEW_PROMO,
     "delay_min": settings.AUTO_NEW_PROMO_DELAY},
    {"key": "auto_low_stock", "name": "Estoque Baixo", "enabled": settings.AUTO_LOW_STOCK,
     "threshold": settings.AUTO_LOW_STOCK_THRESHOLD},
    {"key": "auto_heartbeat", "name": "Heartbeat", "enabled": settings.AUTO_HEARTBEAT},
]


# ============================================================
# 🚀 EXECUÇÃO DO SEED
# ============================================================
async def _seed_admins() -> int:
    """Cria os admins da whitelist do .env (se não existirem)."""
    created = 0
    admin_ids = settings.admin_ids_list or []
    if not admin_ids:
        logger.warning("Nenhum admin em ADMIN_IDS — pulando seed de admins.")
        return 0

    async with session_scope() as session:
        for tg_id in admin_ids:
            r = await session.execute(
                select(Admin).where(Admin.telegram_id == int(tg_id))
            )
            existing = r.scalar_one_or_none()
            if existing is not None:
                continue
            is_main = int(tg_id) == settings.MAIN_ADMIN_ID
            session.add(
                Admin(
                    telegram_id=int(tg_id),
                    is_main_admin=is_main,
                    permissions=["*"] if is_main else [],
                    last_login_at=None,
                )
            )
            created += 1
    return created


async def _seed_roles() -> int:
    created = 0
    async with session_scope() as session:
        for spec in DEFAULT_ROLES:
            r = await session.execute(
                select(Role).where(Role.name == spec["name"])
            )
            if r.scalar_one_or_none() is not None:
                continue
            session.add(
                Role(
                    name=spec["name"],
                    description=spec["description"],
                    permissions=spec["permissions"],
                    is_system=spec["is_system"],
                )
            )
            created += 1
    return created


async def _seed_categories() -> int:
    created = 0
    async with session_scope() as session:
        for cat in DEFAULT_CATEGORIES:
            r = await session.execute(
                select(Category).where(Category.slug == cat["slug"])
            )
            if r.scalar_one_or_none() is not None:
                continue
            session.add(
                Category(
                    name=cat["name"],
                    slug=cat["slug"],
                    emoji=cat.get("emoji"),
                    order=int(cat.get("order", 0)),
                    visible=True,
                )
            )
            created += 1
    return created


async def _seed_messages() -> int:
    created = 0
    async with session_scope() as session:
        for msg in DEFAULT_MESSAGES:
            r = await session.execute(
                select(MessageTemplate).where(MessageTemplate.key == msg["key"])
            )
            if r.scalar_one_or_none() is not None:
                continue
            session.add(
                MessageTemplate(
                    key=msg["key"],
                    screen=msg.get("screen") or msg["key"],
                    content=msg["content"],
                    parse_mode="HTML",
                    version=1,
                    active=True,
                )
            )
            created += 1
    return created


async def _seed_buttons() -> int:
    created = 0
    async with session_scope() as session:
        for btn in DEFAULT_BUTTONS:
            r = await session.execute(
                select(ButtonTemplate)
                .where(ButtonTemplate.screen == btn["screen"])
                .where(ButtonTemplate.key == btn["key"])
            )
            if r.scalar_one_or_none() is not None:
                continue
            session.add(
                ButtonTemplate(
                    screen=btn["screen"],
                    key=btn["key"],
                    text=btn["text"],
                    emoji=btn.get("emoji"),
                    type=btn.get("type", "callback"),
                    callback_data=btn.get("callback_data"),
                    url=btn.get("url"),
                    webapp_url=btn.get("webapp_url"),
                    row=int(btn.get("row", 1)),
                    position=int(btn.get("position", 0)),
                    order=int(btn.get("order", 0)),
                    visible=True,
                    active=True,
                )
            )
            created += 1
    return created


async def _seed_settings() -> int:
    created = 0
    async with session_scope() as session:
        for spec in DEFAULT_SETTINGS:
            r = await session.execute(
                select(Setting).where(Setting.key == spec["key"])
            )
            if r.scalar_one_or_none() is not None:
                continue
            session.add(
                Setting(
                    key=spec["key"],
                    value=str(spec["value"]),
                    type=spec.get("type", "string"),
                    category=spec.get("category", "general"),
                )
            )
            created += 1
    return created


async def _seed_automations() -> int:
    created = 0
    async with session_scope() as session:
        for spec in DEFAULT_AUTOMATIONS:
            r = await session.execute(
                select(Automation).where(Automation.key == spec["key"])
            )
            if r.scalar_one_or_none() is not None:
                continue
            session.add(
                Automation(
                    key=spec["key"],
                    name=spec["name"],
                    enabled=bool(spec.get("enabled", True)),
                    delay_min=spec.get("delay_min"),
                    threshold=spec.get("threshold"),
                    metadata_={},
                )
            )
            created += 1
    return created


# ============================================================
# 🎯 RUNNER PRINCIPAL
# ============================================================
async def run_seed() -> None:
    """Executa todos os seeds."""
    setup_logger()
    logger.info("🌱 Iniciando seed do banco...")

    await init_db()

    try:
        a = await _seed_admins()
        logger.info(f"✅ Admins criados: {a}")

        r = await _seed_roles()
        logger.info(f"✅ Roles criados: {r}")

        c = await _seed_categories()
        logger.info(f"✅ Categorias criadas: {c}")

        m = await _seed_messages()
        logger.info(f"✅ Mensagens criadas: {m}")

        b = await _seed_buttons()
        logger.info(f"✅ Botões criados: {b}")

        s = await _seed_settings()
        logger.info(f"✅ Configurações criadas: {s}")

        au = await _seed_automations()
        logger.info(f"✅ Automações criadas: {au}")

        total = a + r + c + m + b + s + au
        if total == 0:
            logger.info("🌱 Nada novo para criar — banco já populado.")
        else:
            logger.info(f"🌱 Seed concluído! Total criado: {total}")

    except Exception as exc:
        logger.exception(f"❌ Erro no seed: {exc}")
        raise

    finally:
        await close_db()


def main() -> None:
    try:
        asyncio.run(run_seed())
    except KeyboardInterrupt:
        logger.info("Interrompido pelo usuário.")
    except Exception as exc:
        logger.exception(f"Erro fatal no seed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()


# ============================================================
# 📌 EXPORTS
# ============================================================
__all__ = [
    "run_seed",
    "main",
    "DEFAULT_ROLES",
    "DEFAULT_CATEGORIES",
    "DEFAULT_MESSAGES",
    "DEFAULT_BUTTONS",
    "DEFAULT_SETTINGS",
    "DEFAULT_AUTOMATIONS",
]
