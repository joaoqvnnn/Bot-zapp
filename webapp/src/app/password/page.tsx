/**
 * Larizinha Store — WebApp Next.js
 * Cadastro de Senha de Saque — tela minimalista (branco, sem emojis).
 * Campos: Senha, Repetir Senha, E-mail de recuperação.
 * Força da senha (vermelho/verde) + confirmação final.
 */
"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import {
  Check,
  Eye,
  EyeOff,
  KeyRound,
  Lock,
  Mail,
  ShieldCheck,
} from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { PageLoader } from "@/components/loading";
import {
  useHasWithdrawPassword,
  useSetWithdrawPassword,
} from "@/hooks/useApi";
import { useTelegram } from "@/components/providers";
import { cn, isValidEmail } from "@/lib/utils";

// ============================================================
// 🧮 FORÇA DA SENHA
// ============================================================
interface StrengthResult {
  score: 0 | 1 | 2 | 3 | 4;
  label: string;
  color: string;
  width: string;
}

function calculateStrength(password: string): StrengthResult {
  if (!password) {
    return { score: 0, label: "", color: "bg-border", width: "0%" };
  }

  let score = 0;
  if (password.length >= 6) score++;
  if (password.length >= 10) score++;
  if (/[a-z]/.test(password) && /[A-Z]/.test(password)) score++;
  if (/\d/.test(password)) score++;
  if (/[^A-Za-z0-9]/.test(password)) score++;

  // Mapeia 0-5 → 0-4
  const capped = Math.min(4, score) as 0 | 1 | 2 | 3 | 4;

  const map: Record<number, Omit<StrengthResult, "score">> = {
    0: { label: "Muito fraca", color: "bg-red-500", width: "20%" },
    1: { label: "Fraca", color: "bg-red-500", width: "35%" },
    2: { label: "Média", color: "bg-yellow-500", width: "60%" },
    3: { label: "Forte", color: "bg-emerald-500", width: "80%" },
    4: { label: "Muito forte", color: "bg-emerald-500", width: "100%" },
  };

  return { score: capped, ...map[capped] };
}

// ============================================================
// 🚪 PASSWORD PAGE
// ============================================================
export default function PasswordPage() {
  const router = useRouter();
  const { haptic, hapticImpact, close } = useTelegram();

  const { data: hasData, isLoading: checking } = useHasWithdrawPassword();
  const { trigger: setPassword, isMutating: saving } = useSetWithdrawPassword();

  const [password, setPassword] = React.useState("");
  const [passwordConfirm, setPasswordConfirm] = React.useState("");
  const [recoveryEmail, setRecoveryEmail] = React.useState("");
  const [showPassword, setShowPassword] = React.useState(false);
  const [showConfirm, setShowConfirm] = React.useState(false);
  const [emailOptIn, setEmailOptIn] = React.useState(false);
  const [done, setDone] = React.useState(false);

  const strength = calculateStrength(password);

  // ---------- Validações ----------
  const passwordOk = password.length >= 6;
  const confirmOk = password.length > 0 && password === passwordConfirm;
  const emailOk = !emailOptIn || (recoveryEmail.length > 0 && isValidEmail(recoveryEmail));

  const formValid = passwordOk && confirmOk && emailOk;

  // ---------- Handlers ----------
  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!formValid || saving) return;

    hapticImpact("medium");

    const res = await setPassword({
      password,
      passwordConfirm,
      recoveryEmail: emailOptIn ? recoveryEmail : undefined,
    });

    if (!res?.ok) {
      haptic("error");
      toast.error("Não foi possível cadastrar a senha");
      return;
    }

    haptic("success");
    setDone(true);
  };

  const handleClose = () => {
    hapticImpact("light");
    close();
  };

  // ============================================================
  // ESTADOS
  // ============================================================
  if (checking) {
    return <PageLoader message="Verificando..." />;
  }

  // ---------- Já tem senha ----------
  if (hasData?.has && !done) {
    return (
      <main className="flex min-h-dvh items-center justify-center bg-white px-6">
        <div className="w-full max-w-sm text-center">
          <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-emerald-50">
            <ShieldCheck className="h-8 w-8 text-emerald-600" />
          </div>
          <h1 className="mt-4 text-xl font-semibold text-gray-900">
            Senha já cadastrada
          </h1>
          <p className="mt-2 text-sm text-gray-500">
            Sua senha de saque já está configurada. Se precisar alterá-la,
            utilize a opção &quot;Esqueceu a senha?&quot; na tela de saque.
          </p>
          <Button
            variant="secondary"
            size="lg"
            fullWidth
            className="mt-6"
            onClick={handleClose}
          >
            Fechar
          </Button>
        </div>
      </main>
    );
  }

  // ---------- Tela de sucesso ----------
  if (done) {
    return (
      <main className="flex min-h-dvh items-center justify-center bg-white px-6">
        <div className="w-full max-w-sm text-center">
          <div className="mx-auto flex h-16 w-16 items-center justify-center rounded-full bg-emerald-50">
            <Check className="h-8 w-8 text-emerald-600" />
          </div>
          <h1 className="mt-4 text-xl font-semibold text-gray-900">
            Senha cadastrada com sucesso
          </h1>
          <p className="mt-2 text-sm text-gray-500">
            Use esta senha para autorizar seus saques.
          </p>
          <Button
            variant="default"
            size="lg"
            fullWidth
            className="mt-6"
            onClick={handleClose}
          >
            Fechar
          </Button>
        </div>
      </main>
    );
  }

  // ============================================================
  // FORMULÁRIO
  // ============================================================
  return (
    <main className="min-h-dvh bg-white">
      <header className="border-b border-gray-100 px-6 py-5">
        <div className="mx-auto flex max-w-sm items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-gray-900">
            <KeyRound className="h-5 w-5 text-white" />
          </div>
          <div>
            <h1 className="text-base font-semibold text-gray-900">
              Senha de Saque
            </h1>
            <p className="text-xs text-gray-500">
              Proteja suas retiradas
            </p>
          </div>
        </div>
      </header>

      <form
        onSubmit={handleSubmit}
        className="mx-auto max-w-sm px-6 py-6"
        autoComplete="off"
      >
        {/* =====================================================
            🔐 SENHA
        ===================================================== */}
        <div>
          <label
            htmlFor="password"
            className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-gray-500"
          >
            Senha
          </label>
          <div className="relative">
            <Input
              id="password"
              type={showPassword ? "text" : "password"}
              placeholder="Mínimo 6 caracteres"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              leftIcon={<Lock className="h-4 w-4" />}
              className="border-gray-200 bg-white text-gray-900 placeholder:text-gray-400 focus:border-gray-900 focus:ring-gray-900"
              autoComplete="new-password"
            />
            <button
              type="button"
              onClick={() => setShowPassword((s) => !s)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-700"
              aria-label={showPassword ? "Ocultar senha" : "Mostrar senha"}
            >
              {showPassword ? (
                <EyeOff className="h-4 w-4" />
              ) : (
                <Eye className="h-4 w-4" />
              )}
            </button>
          </div>

          {/* Força da senha */}
          {password.length > 0 && (
            <div className="mt-2">
              <div className="h-1 w-full overflow-hidden rounded-full bg-gray-100">
                <div
                  className={cn(
                    "h-full rounded-full transition-all duration-300",
                    strength.color,
                  )}
                  style={{ width: strength.width }}
                />
              </div>
              <p className="mt-1 text-[11px] text-gray-500">
                Força: <span className="font-medium">{strength.label}</span>
              </p>
            </div>
          )}
        </div>

        {/* =====================================================
            🔐 REPETIR SENHA
        ===================================================== */}
        <div className="mt-4">
          <label
            htmlFor="passwordConfirm"
            className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-gray-500"
          >
            Repetir senha
          </label>
          <div className="relative">
            <Input
              id="passwordConfirm"
              type={showConfirm ? "text" : "password"}
              placeholder="Digite novamente"
              value={passwordConfirm}
              onChange={(e) => setPasswordConfirm(e.target.value)}
              leftIcon={<Lock className="h-4 w-4" />}
              className="border-gray-200 bg-white text-gray-900 placeholder:text-gray-400 focus:border-gray-900 focus:ring-gray-900"
              autoComplete="new-password"
            />
            <button
              type="button"
              onClick={() => setShowConfirm((s) => !s)}
              className="absolute right-3 top-1/2 -translate-y-1/2 text-gray-400 hover:text-gray-700"
              aria-label={showConfirm ? "Ocultar senha" : "Mostrar senha"}
            >
              {showConfirm ? (
                <EyeOff className="h-4 w-4" />
              ) : (
                <Eye className="h-4 w-4" />
              )}
            </button>
          </div>

          {passwordConfirm.length > 0 && (
            <p
              className={cn(
                "mt-1.5 text-[11px]",
                confirmOk ? "text-emerald-600" : "text-red-500",
              )}
            >
              {confirmOk ? "Senhas coincidem" : "Senhas não coincidem"}
            </p>
          )}
        </div>

        {/* =====================================================
            📧 E-MAIL DE RECUPERAÇÃO
        ===================================================== */}
        <div className="mt-6 border-t border-gray-100 pt-6">
          <div className="flex items-start justify-between gap-3">
            <div className="flex-1">
              <label className="flex items-center gap-2 text-sm font-medium text-gray-900">
                <input
                  type="checkbox"
                  checked={emailOptIn}
                  onChange={(e) => setEmailOptIn(e.target.checked)}
                  className="h-4 w-4 rounded border-gray-300 text-gray-900 focus:ring-gray-900"
                />
                Adicionar e-mail de recuperação
              </label>
              <p className="mt-1 text-xs text-gray-500">
                Recomendado. Permite redefinir a senha caso esqueça.
              </p>
            </div>
          </div>

          {emailOptIn && (
            <div className="mt-3">
              <label
                htmlFor="email"
                className="mb-1.5 block text-xs font-medium uppercase tracking-wide text-gray-500"
              >
                E-mail
              </label>
              <Input
                id="email"
                type="email"
                placeholder="seu@email.com"
                value={recoveryEmail}
                onChange={(e) => setRecoveryEmail(e.target.value)}
                leftIcon={<Mail className="h-4 w-4" />}
                className="border-gray-200 bg-white text-gray-900 placeholder:text-gray-400 focus:border-gray-900 focus:ring-gray-900"
                autoComplete="email"
              />
              {recoveryEmail.length > 0 && !isValidEmail(recoveryEmail) && (
                <p className="mt-1.5 text-[11px] text-red-500">
                  E-mail inválido
                </p>
              )}
            </div>
          )}
        </div>

        {/* =====================================================
            ℹ️ AVISO
        ===================================================== */}
        <Card className="mt-6 border-gray-100 bg-gray-50 p-3.5 shadow-none">
          <div className="flex items-start gap-2.5">
            <ShieldCheck className="mt-0.5 h-4 w-4 shrink-0 text-gray-700" />
            <div className="text-xs text-gray-600">
              <p className="font-medium text-gray-900">
                Sua senha é pessoal e intransferível.
              </p>
              <p className="mt-0.5">
                Ela será solicitada em toda solicitação de saque. Nunca
                compartilhe com ninguém.
              </p>
            </div>
          </div>
        </Card>

        {/* =====================================================
            🔘 SUBMIT
        ===================================================== */}
        <Button
          type="submit"
          variant="default"
          size="lg"
          fullWidth
          className="mt-6 bg-gray-900 text-white hover:bg-gray-800"
          loading={saving}
          disabled={!formValid}
          leftIcon={<ShieldCheck className="h-4 w-4" />}
        >
          Cadastrar senha
        </Button>

        <p className="mt-3 text-center text-[11px] text-gray-400">
          Ao cadastrar, você concorda com os termos de uso.
        </p>
      </form>
    </main>
  );
}
