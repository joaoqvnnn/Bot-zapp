/**
 * Larizinha Store — WebApp Next.js
 * Solicitar Saque — tela completa de solicitação.
 * Formulário: valor, tipo de chave PIX, chave, senha + confirmação.
 */
"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import {
  AlertTriangle,
  ArrowLeft,
  Check,
  Eye,
  EyeOff,
  KeyRound,
  Landmark,
  Lock,
  Wallet,
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
import { useMe, useRequestWithdraw, useHasWithdrawPassword } from "@/hooks/useApi";
import { useTelegram } from "@/components/providers";
import { cn, formatBRL, parseMoney, isValidPixKey, isValidCpf, isValidEmail, isValidPhone } from "@/lib/utils";

// ============================================================
// 🎛️ TIPOS DE CHAVE
// ============================================================
type PixKeyType = "cpf" | "email" | "phone" | "random";

interface PixKeyOption {
  key: PixKeyType;
  label: string;
  hint: string;
  placeholder: string;
  validate: (v: string) => boolean;
}

const PIX_OPTIONS: PixKeyOption[] = [
  {
    key: "cpf",
    label: "CPF",
    hint: "Apenas números",
    placeholder: "12345678900",
    validate: (v) => isValidCpf(v),
  },
  {
    key: "email",
    label: "E-mail",
    hint: "seu@email.com",
    placeholder: "seu@email.com",
    validate: (v) => isValidEmail(v),
  },
  {
    key: "phone",
    label: "Telefone",
    hint: "DDD + número",
    placeholder: "11987654321",
    validate: (v) => isValidPhone(v),
  },
  {
    key: "random",
    label: "Aleatória",
    hint: "Chave EVP/UUID",
    placeholder: "a1b2c3d4-...",
    validate: (v) => isValidPixKey(v),
  },
];

// ============================================================
// 🏦 WITHDRAW PAGE
// ============================================================
export default function WithdrawPage() {
  const router = useRouter();
  const { haptic, hapticImpact, hapticSelection } = useTelegram();

  const { data: me, mutate: mutateMe } = useMe();
  const { data: hasPassword } = useHasWithdrawPassword();
  const { trigger: requestWithdraw, isMutating: sending } = useRequestWithdraw();

  const [amountInput, setAmountInput] = React.useState("");
  const [keyType, setKeyType] = React.useState<PixKeyType>("cpf");
  const [pixKey, setPixKey] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [showPassword, setShowPassword] = React.useState(false);
  const [confirmOpen, setConfirmOpen] = React.useState(false);
  const [successOpen, setSuccessOpen] = React.useState(false);

  // ---------- Config ----------
  const MIN_WITHDRAW = 20;
  const MAX_WITHDRAW = 5000;

  // ---------- Valor ----------
  const amount = parseMoney(amountInput);
  const balance = me?.balance ?? 0;

  const amountOk =
    amount !== null &&
    amount >= MIN_WITHDRAW &&
    amount <= MAX_WITHDRAW &&
    amount <= balance;

  const amountErrors = React.useMemo(() => {
    if (!amountInput || amount === null) return null;
    if (amount < MIN_WITHDRAW) return `Valor mínimo: ${formatBRL(MIN_WITHDRAW)}`;
    if (amount > MAX_WITHDRAW) return `Valor máximo: ${formatBRL(MAX_WITHDRAW)}`;
    if (amount > balance) return "Saldo insuficiente";
    return null;
  }, [amountInput, amount, balance]);

  // ---------- Chave ----------
  const currentOption = PIX_OPTIONS.find((o) => o.key === keyType)!;
  const keyOk = currentOption.validate(pixKey.trim());

  // ---------- Senha ----------
  const passwordOk = password.length >= 4;

  // ---------- Form ----------
  const formValid = amountOk && keyOk && passwordOk && !sending;

  // ---------- Handlers ----------
  const handleSelectKey = (key: PixKeyType) => {
    hapticSelection();
    setKeyType(key);
    setPixKey("");
  };

  const handleReview = (e: React.FormEvent) => {
    e.preventDefault();
    if (!formValid) {
      haptic("error");
      return;
    }
    hapticImpact("light");
    setConfirmOpen(true);
  };

  const handleConfirm = async () => {
    if (!amount || !formValid) return;

    hapticImpact("medium");

    const res = await requestWithdraw({
      amount,
      pixKey: pixKey.trim(),
      pixKeyType: currentOption.label,
      password,
    });

    if (!res) {
      haptic("error");
      toast.error("Não foi possível solicitar o saque");
      setConfirmOpen(false);
      return;
    }

    haptic("success");
    setConfirmOpen(false);
    setSuccessOpen(true);
    await mutateMe();
  };

  const handleDone = () => {
    setSuccessOpen(false);
    router.push("/orders");
  };

  // ---------- Sem saldo suficiente ----------
  const insufficientBalance = balance < MIN_WITHDRAW;

  // ============================================================
  // RENDER
  // ============================================================
  return (
    <>
      <Topbar title="Sacar" showBack />

      <main className="px-4 pb-28 pt-3">
        {/* =====================================================
            💰 SALDO
        ===================================================== */}
        <Card className="relative overflow-hidden border-none bg-gradient-to-br from-primary/15 to-accent/10 p-4">
          <div className="flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
            <Wallet className="h-3.5 w-3.5" />
            Saldo disponível
          </div>
          <div className="mt-1 font-mono text-2xl font-bold text-primary tabular-nums">
            {formatBRL(balance)}
          </div>
          <p className="mt-1 text-xs text-muted-foreground">
            Saque mínimo de {formatBRL(MIN_WITHDRAW)}
          </p>
        </Card>

        {/* =====================================================
            ⚠️ SEM SENHA
        ===================================================== */}
        {hasPassword && !hasPassword.has && (
          <Card className="mt-4 border-yellow-500/30 bg-yellow-500/10 p-3.5">
            <div className="flex items-start gap-2.5">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-yellow-500" />
              <div className="flex-1">
                <p className="text-sm font-semibold text-yellow-500">
                  Senha de saque não cadastrada
                </p>
                <p className="mt-0.5 text-xs text-foreground/70">
                  Cadastre uma senha antes de solicitar saques.
                </p>
              </div>
            </div>
            <Link
              href="/password"
              className="mt-3 inline-flex h-9 items-center justify-center rounded-lg border border-yellow-500/40 bg-yellow-500/10 px-3 text-xs font-semibold text-yellow-500"
            >
              Cadastrar senha agora
            </Link>
          </Card>
        )}

        {/* =====================================================
            ⚠️ SALDO INSUFICIENTE
        ===================================================== */}
        {insufficientBalance && (
          <Card className="mt-4 border-destructive/30 bg-destructive/10 p-3.5">
            <div className="flex items-start gap-2.5">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" />
              <div className="flex-1">
                <p className="text-sm font-semibold text-destructive">
                  Saldo insuficiente para saque
                </p>
                <p className="mt-0.5 text-xs text-foreground/70">
                  Você precisa de pelo menos {formatBRL(MIN_WITHDRAW)} para sacar.
                </p>
              </div>
            </div>
          </Card>
        )}

        {/* =====================================================
            FORM
        ===================================================== */}
        <form onSubmit={handleReview} className="mt-4">
          {/* ---------- Valor ---------- */}
          <div>
            <label className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Valor do saque
            </label>
            <Input
              inputMode="decimal"
              placeholder="0,00"
              value={amountInput}
              onChange={(e) =>
                setAmountInput(e.target.value.replace(/[^\d,.]/g, ""))
              }
              leftIcon={<span className="text-sm font-medium">R$</span>}
              error={amountErrors ?? undefined}
              rightIcon={
                balance > 0 ? (
                  <button
                    type="button"
                    onClick={() => {
                      hapticSelection();
                      setAmountInput(String(balance));
                    }}
                    className="pointer-events-auto text-[11px] font-semibold text-primary"
                  >
                    MÁX
                  </button>
                ) : undefined
              }
            />
          </div>

          {/* ---------- Tipo de chave ---------- */}
          <div className="mt-4">
            <label className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Tipo de chave PIX
            </label>
            <div className="grid grid-cols-4 gap-2">
              {PIX_OPTIONS.map((opt) => {
                const active = keyType === opt.key;
                return (
                  <button
                    key={opt.key}
                    type="button"
                    onClick={() => handleSelectKey(opt.key)}
                    className={cn(
                      "rounded-xl border px-1 py-2.5 text-xs font-semibold",
                      "transition-all active:scale-95",
                      active
                        ? "border-primary bg-primary/15 text-primary"
                        : "border-border bg-card text-foreground/70 hover:border-primary/40",
                    )}
                  >
                    {opt.label}
                  </button>
                );
              })}
            </div>
          </div>

          {/* ---------- Chave ---------- */}
          <div className="mt-3">
            <label className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Chave PIX
            </label>
            <Input
              placeholder={currentOption.placeholder}
              value={pixKey}
              onChange={(e) => setPixKey(e.target.value)}
              leftIcon={<KeyRound className="h-4 w-4" />}
              error={
                pixKey.length > 0 && !keyOk
                  ? `Formato inválido. ${currentOption.hint}`
                  : undefined
              }
            />
            <p className="mt-1.5 text-[11px] text-muted-foreground">
              {currentOption.hint}
            </p>
          </div>

          {/* ---------- Senha ---------- */}
          <div className="mt-4">
            <label className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Senha de saque
            </label>
            <div className="relative">
              <Input
                type={showPassword ? "text" : "password"}
                placeholder="Digite sua senha"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                leftIcon={<Lock className="h-4 w-4" />}
              />
              <button
                type="button"
                onClick={() => setShowPassword((s) => !s)}
                className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground"
                aria-label={showPassword ? "Ocultar senha" : "Mostrar senha"}
              >
                {showPassword ? (
                  <EyeOff className="h-4 w-4" />
                ) : (
                  <Eye className="h-4 w-4" />
                )}
              </button>
            </div>
          </div>

          {/* ---------- Info ---------- */}
          <Card className="mt-5 border-border/60 bg-secondary/40 p-3.5 shadow-none">
            <div className="flex items-start gap-2.5">
              <Landmark className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
              <div className="text-xs text-muted-foreground">
                <p className="font-medium text-foreground/80">
                  Processamento em até 24h úteis.
                </p>
                <p className="mt-0.5">
                  O valor será enviado para a chave PIX informada. Confira
                  antes de solicitar.
                </p>
              </div>
            </div>
          </Card>

          {/* ---------- Submit ---------- */}
          <Button
            type="submit"
            variant="default"
            size="lg"
            fullWidth
            className="mt-5"
            disabled={!formValid}
            leftIcon={<ArrowLeft className="h-4 w-4 rotate-180" />}
          >
            Revisar saque
          </Button>
        </form>
      </main>

      {/* =====================================================
          ✅ DIALOG DE CONFIRMAÇÃO
      ===================================================== */}
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle className="text-center">
              Confirmar saque
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-3">
            <div className="rounded-xl border border-border bg-secondary/40 p-3.5">
              <Row label="Valor" value={formatBRL(amount ?? 0)} mono />
              <Row label="Tipo de chave" value={currentOption.label} />
              <Row label="Chave" value={pixKey} mono small />
            </div>

            <div className="flex items-start gap-2 rounded-xl border border-yellow-500/30 bg-yellow-500/10 p-3">
              <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-yellow-500" />
              <p className="text-xs text-foreground/70">
                Confira a chave PIX. Envios para chaves incorretas{" "}
                <b>não podem ser revertidos</b>.
              </p>
            </div>

            <div className="grid grid-cols-2 gap-2">
              <Button
                variant="secondary"
                size="default"
                fullWidth
                onClick={() => setConfirmOpen(false)}
              >
                Cancelar
              </Button>
              <Button
                variant="default"
                size="default"
                fullWidth
                loading={sending}
                onClick={handleConfirm}
                leftIcon={<Check className="h-4 w-4" />}
              >
                Confirmar
              </Button>
            </div>
          </div>
        </DialogContent>
      </Dialog>

      {/* =====================================================
          🎉 SUCESSO
      ===================================================== */}
      <Dialog open={successOpen} onOpenChange={setSuccessOpen}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle className="text-center">
              Saque solicitado!
            </DialogTitle>
          </DialogHeader>

          <div className="space-y-3 text-center">
            <div className="mx-auto flex h-14 w-14 items-center justify-center rounded-full bg-emerald-500/15">
              <Check className="h-7 w-7 text-emerald-500" />
            </div>

            <p className="text-sm text-muted-foreground">
              Seu saque foi enviado para análise. Você será notificado no
              Telegram assim que for processado.
            </p>

            <div className="rounded-xl border border-border bg-secondary/40 p-3">
              <Row label="Valor" value={formatBRL(amount ?? 0)} mono />
              <Row label="Chave" value={pixKey} mono small />
            </div>

            <Button
              variant="default"
              size="default"
              fullWidth
              onClick={handleDone}
            >
              Ver pedidos
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

// ============================================================
// 🧩 ROW DE RESUMO
// ============================================================
function Row({
  label,
  value,
  mono,
  small,
}: {
  label: string;
  value: string;
  mono?: boolean;
  small?: boolean;
}) {
  return (
    <div className="flex items-start justify-between gap-3 py-1.5">
      <span className="text-xs text-muted-foreground">{label}</span>
      <span
        className={cn(
          "max-w-[60%] break-all text-right text-xs font-semibold",
          mono && "font-mono",
          small && "text-[11px]",
        )}
      >
        {value}
      </span>
    </div>
  );
}
