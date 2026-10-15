/**
 * Larizinha Store — WebApp Next.js
 * Toaster (sonner) — exibe notificações toast
 */
"use client";

import { Toaster as Sonner, type ToasterProps } from "sonner";

const Toaster = ({ ...props }: ToasterProps) => {
  return (
    <Sonner
      className="toaster group"
      position="top-center"
      toastOptions={{
        classNames: {
          toast:
            "group toast group-[.toaster]:bg-card group-[.toaster]:text-card-foreground group-[.toaster]:border-border group-[.toaster]:shadow-lg",
          description: "group-[.toast]:text-muted-foreground",
          actionButton:
            "group-[.toast]:bg-primary group-[.toast]:text-primary-foreground",
          cancelButton:
            "group-[.toast]:bg-muted group-[.toast]:text-muted-foreground",
          success:
            "group-[.toaster]:bg-emerald-500/10 group-[.toaster]:text-emerald-500 group-[.toaster]:border-emerald-500/30",
          error:
            "group-[.toaster]:bg-destructive/10 group-[.toaster]:text-destructive group-[.toaster]:border-destructive/30",
          warning:
            "group-[.toaster]:bg-yellow-500/10 group-[.toaster]:text-yellow-500 group-[.toaster]:border-yellow-500/30",
        },
      }}
      {...props}
    />
  );
};

export { Toaster };
