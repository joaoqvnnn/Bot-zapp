/**
 * Larizinha Store — WebApp Next.js
 * Carteira — recarregar saldo via PIX + histórico de transações.
 */
"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  ArrowDownLeft,
  ArrowUpRight,
  Check,
  Clock,
  Copy,
  Gift,
  Loader2,
  QrCode,
  RefreshCw,
  ShoppingCart,
  Wallet,
  XCircle,
  Zap,
} from "lucide-react";
import { toast } from "sonner";

import { Topbar } from "@/components/topbar";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { SkeletonList } from "@/components/ui/skeleton";
import { EmptyState, ErrorState } from "@/components/loading";
import {
  useMe,
  useCreatePix,
  usePix,
  useTransactions,
} from "@/hooks/useApi";
import { useTelegram } from "@/components/providers";
import { cn, formatBRL, formatDate, copyToClipboard, parseMoney } from "@/lib/utils";
import type { PixCharge } from "@/lib/api";

// ============================================================
// 💰 VALORES RÁPIDOS
// ============================================================
const QUICK_AMOUNTS = [30, 50, 100, 200];

// ============================================================
// 💰 WALLET PAGE
// ============================================================
export default function WalletPage() {
  const router = useRouter();
  const { haptic, hapticImpact, hapticSelection } = useTelegram();

  const { data: me, mutate: mutateMe } = useMe();
  const { data: txs, isLoading: txLoading, error: txError, mutate: mutateTx } = useTransactions(1, 20);
  const { trigger: createPix, isMutating: creatingPix } = useCreatePix();

  const [amountInput, setAmountInput] = React.useState("");
  const [activePix, setActivePix] = React.useState<PixCharge | null>(null);
  const [pixDialogOpen, setPixDialogOpen] = React.useState(false);

  // ---------- Valor selecionado ----------
  const selectedAmount = parseMoney(amountInput);
  const isValid = selectedAmount !== null && selectedAmount >= 10;

  // ---------- Handlers ----------
  const handleQuickAmount = (value: number) => {
    hapticSelection();
    setAmountInput(String(value));
  };

  const handleCreatePix = async (value: number) => {
    if (!value || value < 10) {
      haptic("error");
      toast.error("Valor mínimo: R$ 10,00");
      return;
    }
    hapticImpact("medium");

    const pix = await createPix({ amount: value });
    if (!pix) {
      haptic("error");
      toast.error("Não foi possível gerar o PIX");
      return;
    }

    haptic("success");
    setActivePix(pix);
    setPixDialogOpen(true);
    setAmountInput("");
  };

  const handlePixPaid = async () => {
    haptic("success");
    toast.success("Pagamento confirmado!");
    setPixDialogOpen(false);
    setActivePix(null);
    await Promise.all([mutateMe(), mutateTx()]);
  };

  return (
    <>
      <Topbar title="Carteira" />

      <main className="px-4 pb-28 pt-3">
        {/* =====================================================
            💰 SALDO EM DESTAQUE
        ===================================================== */}
        <Card className="relative overflow-hidden border-none bg-gradient-to-br from-primary/20 via-primary/10 to-accent/15 p-5">
          <div className="pointer-events-none absolute -right-8 -top-8 h-32 w-32 rounded-full bg-primary/20 blur-3xl" />

          <div className="relative">
            <div className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-foreground/70">
              <Wallet className="h-3.5 w-3.5" />
              Saldo disponível
            </div>
            <div className="mt-1.5 font-mono text-3xl font-bold tabular-nums text-primary">
              {formatBRL(me?.balance ?? 0)}
            </div>

            {me && me.balanceLocked > 0 && (
              <div className="mt-1 text-xs text-muted-foreground">
                Bloqueado: {formatBRL(me.balanceLocked)}
              </div>
            )}

            <div className="mt-4 grid grid-cols-2 gap-2">
              <StatMini
                label="Total depositado"
                value={formatBRL(me?.totalDeposited ?? 0)}
                icon={<ArrowDownLeft className="h-3 w-3 text-emerald-500" />}
              />
              <StatMini
                label="Total gasto"
                value={formatBRL(me?.totalSpent ?? 0)}
                icon={<ArrowUpRight className="h-3 w-3 text-blue-500" />}
              />
            </div>
          </div>
        </Card>

        {/* =====================================================
            💠 RECARREGAR VIA PIX
        ===================================================== */}
        <section className="mt-5">
          <SectionHeader
            title="Recarregar via PIX"
            icon={<Zap className="h-4 w-4 text-primary" />}
            subtitle="Liberação instantânea"
          />

          {/* ---------- Valores rápidos ---------- */}
          <div className="mt-3 grid grid-cols-4 gap-2">
            {QUICK_AMOUNTS.map((v) => (
              <button
                key={v}
                type="button"
                onClick={() => handleQuickAmount(v)}
                className={cn(
                  "rounded-xl border px-2 py-2.5 text-sm font-semibold transition-all active:scale-95",
                  selectedAmount === v
                    ? "border-primary bg-primary/15 text-primary"
                    : "border-border bg-card text-foreground/80 hover:border-primary/40",
                )}
              >
                R$ {v}
              </button>
            ))}
          </div>

          {/* ---------- Campo livre ---------- */}
          <div className="mt-3">
            <Input
              placeholder="Valor personalizado (ex: 25.50)"
              inputMode="decimal"
              value={amountInput}
              onChange={(e) => {
                const v = e.target.value.replace(/[^\d,.]/g, "");
                setAmountInput(v);
              }}
              leftIcon={<span className="text-sm font-medium">R$</span>}
            />
            <p className="mt-1.5 text-xs text-muted-foreground">
              Valor mínimo: <b>R$ 10,00</b>
            </p>
          </div>

          <Button
            variant="default"
            size="lg"
            fullWidth
            className="mt-3"
            loading={creatingPix}
            disabled={!isValid}
            onClick={() => selectedAmount && handleCreatePix(selectedAmount)}
            leftIcon={<QrCode className="h-4 w-4" />}
          >
            Gerar PIX
          </Button>
        </section>

        {/* =====================================================
            📜 HISTÓRICO
        ===================================================== */}
        <section className="mt-6">
          <SectionHeader
            title="Histórico"
            subtitle={
              txs ? `${txs.length} ${txs.length === 1 ? "movimentação" : "movimentações"}` : undefined
            }
          />

          <div className="mt-3">
            {txLoading ? (
              <SkeletonList count={5} />
            ) : txError ? (
              <ErrorState
                title="Erro ao carregar"
                onRetry={() => mutateTx()}
              />
            ) : txs && txs.length > 0 ? (
              <Card className="divide-y divide-border/60 p-1">
                {(txs as TransactionItem[]).map((tx) => (
                  <TransactionRow key={tx.id} tx={tx} />
                ))}
              </Card>
            ) : (
              <EmptyState
                icon={<Wallet className="h-7 w-7" />}
                title="Nenhuma movimentação"
                description="Suas recargas e compras aparecerão aqui."
              />
            )}
          </div>
        </section>
      </main>

      {/* =====================================================
          💠 DIALOG DO PIX
      ===================================================== */}
      <PixDialog
        open={pixDialogOpen}
        onOpenChange={(o) => {
          setPixDialogOpen(o);
          if (!o) setActivePix(null);
        }}
        pix={activePix}
        onPaid={handlePixPaid}
      />
    </>
  );
}

// ============================================================
// 💠 PIX DIALOG
// ============================================================
function PixDialog({
  open,
  onOpenChange,
  pix,
  onPaid,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  pix: PixCharge | null;
  onPaid: () => void;
}) {
  const { haptic, hapticImpact } = useTelegram();
  const [copied, setCopied] = React.useState(false);

  // Polling do status
  const { data: currentPix } = usePix(open ? pix?.txid : null);

  const status = currentPix?.status ?? pix?.status ?? "PENDING";

  // Detecta pagamento
  React.useEffect(() => {
    if (open && status === "PAID") {
      haptic("success");
      onPaid();
    }
  }, [open, status, haptic, onPaid]);

  const handleCopy = async () => {
    if (!pix?.copyPaste) return;
    const ok = await copyToClipboard(pix.copyPaste);
    if (ok) {
      hapticImpact("light");
      setCopied(true);
      toast.success("Código copiado!");
      setTimeout(() => setCopied(false), 2500);
    } else {
      toast.error("Não foi possível copiar");
    }
  };

  const handleCopyQrText = async () => {
    if (!pix?.copyPaste) return;
    await copyToClipboard(pix.copyPaste);
    toast.success("Código copiado!");
  };

  if (!pix) return null;

  const isExpired = status === "EXPIRED";
  const isCanceled = status === "CANCELED";
  const isPaid = status === "PAID";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm">
        <DialogHeader>
          <DialogTitle className="text-center">
            {isPaid
              ? "✅ Pagamento confirmado!"
              : isExpired
                ? "⌛️ PIX expirado"
                : isCanceled
                  ? "❌ PIX cancelado"
                  : "Pague com PIX"}
          </DialogTitle>
        </DialogHeader>

        <div className="space-y-3">
          {/* Valor */}
          <div className="text-center">
            <div className="text-xs uppercase tracking-wide text-muted-foreground">
              Valor
            </div>
            <div className="font-mono text-2xl font-bold text-primary">
              {formatBRL(pix.amount)}
            </div>
            {pix.bonus > 0 && (
              <div className="mt-0.5 inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2 py-0.5 text-[10px] font-bold text-emerald-500">
                <Gift className="h-3 w-3" />
                + {formatBRL(pix.bonus)} bônus
              </div>
            )}
          </div>

          {/* QR / status */}
          {isPaid ? (
            <div className="flex flex-col items-center gap-3 py-4">
              <div className="flex h-14 w-14 items-center justify-center rounded-full bg-emerald-500/15">
                <Check className="h-7 w-7 text-emerald-500" />
              </div>
              <p className="text-center text-sm text-muted-foreground">
                Seu saldo foi creditado com sucesso.
              </p>
            </div>
          ) : isExpired || isCanceled ? (
            <div className="flex flex-col items-center gap-3 py-4">
              <div className="flex h-14 w-14 items-center justify-center rounded-full bg-destructive/15">
                <XCircle className="h-7 w-7 text-destructive" />
              </div>
              <p className="text-center text-sm text-muted-foreground">
                {isExpired
                  ? "O tempo limite foi excedido. Gere um novo PIX."
                  : "Este PIX foi cancelado."}
              </p>
            </div>
          ) : (
            <>
              {/* QR Code */}
              {pix.qrCodeBase64 && (
                <div className="mx-auto flex h-52 w-52 items-center justify-center rounded-2xl bg-white p-3">
                  {/* eslint-disable-next-line @next/next/no-img-element */}
                  <img
                    src={
                      pix.qrCodeBase64.startsWith("data:")
                        ? pix.qrCodeBase64
                        : `data:image/png;base64,${pix.qrCodeBase64}`
                    }
                    alt="QR Code PIX"
                    className="h-full w-full object-contain"
                    onClick={handleCopyQrText}
                  />
                </div>
              )}

              {/* Expiração */}
              <div className="flex items-center justify-center gap-1.5 text-xs text-muted-foreground">
                <Clock className="h-3 w-3" />
                Expira em alguns minutos
              </div>

              {/* Copia e cola */}
              <div className="rounded-xl border border-border bg-secondary/40 p-3">
                <div className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                  Copia e cola
                </div>
                <div className="mt-1 break-all font-mono text-[11px] leading-snug text-foreground/80 line-clamp-3">
                  {pix.copyPaste}
                </div>
              </div>

              <Button
                variant={copied ? "success" : "default"}
                size="default"
                fullWidth
                onClick={handleCopy}
                leftIcon={
                  copied ? (
                    <Check className="h-4 w-4" />
                  ) : (
                    <Copy className="h-4 w-4" />
                  )
                }
              >
                {copied ? "Copiado!" : "Copiar código PIX"}
              </Button>
            </>
          )}

          {/* Rodapé de status */}
          {!isPaid && !isExpired && !isCanceled && (
            <div className="flex items-center justify-center gap-1.5 pt-1 text-xs text-muted-foreground">
              <Loader2 className="h-3 w-3 animate-spin" />
              Aguardando pagamento...
            </div>
          )}

          {isPaid && (
            <Button
              variant="default"
              size="default"
              fullWidth
              onClick={() => onOpenChange(false)}
            >
              Continuar
            </Button>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}

// ============================================================
// 📜 TRANSACTION ROW
// ============================================================
interface TransactionItem {
  id: number;
  type: string;
  amount: number | string;
  description: string | null;
  createdAt: string;
}

function TransactionRow({ tx }: { tx: TransactionItem }) {
  const amount = Number(tx.amount ?? 0);
  const isCredit = [
    "DEPOSIT",
    "REFUND",
    "COMMISSION",
    "GIFT",
    "ADMIN_CREDIT",
    "BONUS",
  ].includes(tx.type);

  const meta = getTxMeta(tx.type);

  return (
    <div className="flex items-center gap-3 px-3 py-3">
      <div
        className={cn(
          "flex h-9 w-9 shrink-0 items-center justify-center rounded-full",
          isCredit ? "bg-emerald-500/15 text-emerald-500" : "bg-destructive/15 text-destructive",
        )}
      >
        {meta.icon}
      </div>

      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{meta.label}</div>
        <div className="truncate text-[11px] text-muted-foreground">
          {tx.description || formatDate(tx.createdAt, { withTime: true })}
        </div>
      </div>

      <div
        className={cn(
          "shrink-0 text-right font-mono text-sm font-bold tabular-nums",
          isCredit ? "text-emerald-500" : "text-destructive",
        )}
      >
        {isCredit ? "+" : "-"} {formatBRL(Math.abs(amount))}
      </div>
    </div>
  );
}

function getTxMeta(type: string): { label: string; icon: React.ReactNode } {
  const map: Record<string, { label: string; icon: React.ReactNode }> = {
    DEPOSIT: { label: "Recarga PIX", icon: <ArrowDownLeft className="h-4 w-4" /> },
    WITHDRAW: { label: "Saque", icon: <ArrowUpRight className="h-4 w-4" /> },
    PURCHASE: { label: "Compra", icon: <ShoppingCart className="h-4 w-4" /> },
    REFUND: { label: "Estorno", icon: <RefreshCw className="h-4 w-4" /> },
    COMMISSION: { label: "Comissão", icon: <Gift className="h-4 w-4" /> },
    GIFT: { label: "Gift Card", icon: <Gift className="h-4 w-4" /> },
    ADMIN_CREDIT: { label: "Crédito admin", icon: <ArrowDownLeft className="h-4 w-4" /> },
    ADMIN_DEBIT: { label: "Débito admin", icon: <ArrowUpRight className="h-4 w-4" /> },
    BONUS: { label: "Bônus", icon: <Gift className="h-4 w-4" /> },
  };
  return map[type] ?? { label: type, icon: <Wallet className="h-4 w-4" /> };
}

// ============================================================
// 🧩 HELPERS DE UI
// ============================================================
function StatMini({
  label,
  value,
  icon,
}: {
  label: string;
  value: string;
  icon?: React.ReactNode;
}) {
  return (
    <div className="rounded-xl border border-border/60 bg-card/60 px-3 py-2">
      <div className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-muted-foreground">
        {icon}
        {label}
      </div>
      <div className="mt-0.5 font-mono text-sm font-semibold tabular-nums">
        {value}
      </div>
    </div>
  );
}

function SectionHeader({
  title,
  subtitle,
  icon,
}: {
  title: string;
  subtitle?: string;
  icon?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between">
      <div className="flex items-center gap-1.5">
        {icon}
        <h2 className="text-sm font-semibold uppercase tracking-wide">
          {title}
        </h2>
      </div>
      {subtitle && (
        <span className="text-xs text-muted-foreground">{subtitle}</span>
      )}
    </div>
  );
}
