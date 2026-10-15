# 🛍️ Larizinha Store

Bot de vendas automáticas para Telegram com painel administrativo completo, pagamento via PIX (Mercado Pago), programa de afiliados, sistema de tickets, jogos, WebApp e muito mais.

---

## 📚 Índice

- [Stack](#-stack)
- [Estrutura do projeto](#-estrutura-do-projeto)
- [Como rodar localmente](#-como-rodar-localmente)
- [Como fazer deploy no Render](#-como-fazer-deploy-no-render)
- [Configuração inicial](#-configuração-inicial)
- [Mercado Pago (PIX)](#-mercado-pago-pix)
- [SMTP (E-mail)](#-smtp-e-mail)
- [Painel Admin — 30 módulos](#-painel-admin--30-módulos)
- [Fluxo do membro](#-fluxo-do-membro)
- [Variáveis de ambiente](#-variáveis-de-ambiente)
- [Troubleshooting](#-troubleshooting)

---

## 🧰 Stack

| Camada | Tecnologia |
|--------|------------|
| **Bot** | Python 3.12 + Aiogram 3.13 |
| **Banco** | PostgreSQL 16 |
| **Cache / Filas / Travas** | Redis 7 |
| **Pagamentos** | Mercado Pago (PIX) |
| **E-mail** | SMTP (aiosmtplib) |
| **API** | TypeScript + Fastify + Prisma |
| **WebApp** | Next.js 14 + React 18 + Tailwind |
| **Deploy** | Render (Blueprint) |
| **Containerização** | Docker + docker-compose |

---

## 📁 Estrutura do projeto
