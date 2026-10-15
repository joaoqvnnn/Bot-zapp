/**
 * Larizinha Store — WebApp Next.js
 * Componente Input (padrão shadcn/ui adaptado)
 */
import * as React from "react";

import { cn } from "@/lib/utils";

// ============================================================
// ⌨️ INPUT
// ============================================================
export interface InputProps
  extends React.InputHTMLAttributes<HTMLInputElement> {
  leftIcon?: React.ReactNode;
  rightIcon?: React.ReactNode;
  error?: string | boolean;
}

const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, type, leftIcon, rightIcon, error, ...props }, ref) => {
    const hasError = Boolean(error);
    const errorText = typeof error === "string" ? error : undefined;

    return (
      <div className="w-full">
        <div className="relative">
          {leftIcon && (
            <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground [&_svg]:size-4">
              {leftIcon}
            </span>
          )}

          <input
            ref={ref}
            type={type}
            className={cn(
              "flex h-11 w-full rounded-xl border bg-card px-4 py-3 text-base",
              "border-border text-foreground placeholder:text-muted-foreground",
              "transition-colors",
              "focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary",
              "disabled:cursor-not-allowed disabled:opacity-50",
              "file:border-0 file:bg-transparent file:text-sm file:font-medium",
              leftIcon && "pl-9",
              rightIcon && "pr-10",
              hasError &&
                "border-destructive focus:border-destructive focus:ring-destructive",
              className,
            )}
            {...props}
          />

          {rightIcon && (
            <span className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground [&_svg]:size-4">
              {rightIcon}
            </span>
          )}
        </div>

        {errorText && (
          <p className="mt-1 text-xs text-destructive">{errorText}</p>
        )}
      </div>
    );
  },
);
Input.displayName = "Input";

// ============================================================
// 📝 TEXTAREA
// ============================================================
export interface TextareaProps
  extends React.TextareaHTMLAttributes<HTMLTextAreaElement> {
  error?: string | boolean;
}

const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(
  ({ className, error, ...props }, ref) => {
    const hasError = Boolean(error);
    const errorText = typeof error === "string" ? error : undefined;

    return (
      <div className="w-full">
        <textarea
          ref={ref}
          className={cn(
            "flex min-h-[80px] w-full rounded-xl border bg-card px-4 py-3 text-base",
            "border-border text-foreground placeholder:text-muted-foreground",
            "resize-none",
            "focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary",
            "disabled:cursor-not-allowed disabled:opacity-50",
            hasError &&
              "border-destructive focus:border-destructive focus:ring-destructive",
            className,
          )}
          {...props}
        />
        {errorText && (
          <p className="mt-1 text-xs text-destructive">{errorText}</p>
        )}
      </div>
    );
  },
);
Textarea.displayName = "Textarea";

// ============================================================
// 📌 EXPORTS
// ============================================================
export { Input, Textarea };
