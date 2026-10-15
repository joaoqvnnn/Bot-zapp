/**
 * Larizinha Store — WebApp Next.js
 * Skeleton — placeholders animados para loading
 */
import { cn } from "@/lib/utils";

function Skeleton({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "animate-pulse rounded-lg bg-secondary/60",
        className,
      )}
      {...props}
    />
  );
}

// ============================================================
// 📦 SKELETONS ESPECÍFICOS
// ============================================================
function SkeletonText({
  lines = 3,
  className,
}: {
  lines?: number;
  className?: string;
}) {
  return (
    <div className={cn("space-y-2", className)}>
      {Array.from({ length: lines }).map((_, i) => (
        <Skeleton
          key={i}
          className={cn(
            "h-4",
            i === lines - 1 ? "w-2/3" : "w-full",
          )}
        />
      ))}
    </div>
  );
}

function SkeletonProductCard() {
  return (
    <div className="rounded-2xl border border-border bg-card p-3">
      <Skeleton className="mb-3 aspect-video w-full rounded-xl" />
      <Skeleton className="mb-2 h-4 w-3/4" />
      <Skeleton className="h-4 w-1/2" />
    </div>
  );
}

function SkeletonProductGrid({
  count = 4,
}: {
  count?: number;
}) {
  return (
    <div className="grid grid-cols-2 gap-3">
      {Array.from({ length: count }).map((_, i) => (
        <SkeletonProductCard key={i} />
      ))}
    </div>
  );
}

function SkeletonRow() {
  return (
    <div className="flex items-center gap-3 py-3">
      <Skeleton className="h-10 w-10 rounded-full" />
      <div className="flex-1 space-y-2">
        <Skeleton className="h-4 w-1/3" />
        <Skeleton className="h-3 w-1/2" />
      </div>
      <Skeleton className="h-6 w-16 rounded-md" />
    </div>
  );
}

function SkeletonList({ count = 5 }: { count?: number }) {
  return (
    <div className="divide-y divide-border">
      {Array.from({ length: count }).map((_, i) => (
        <SkeletonRow key={i} />
      ))}
    </div>
  );
}

// ============================================================
// 📌 EXPORTS
// ============================================================
export {
  Skeleton,
  SkeletonText,
  SkeletonProductCard,
  SkeletonProductGrid,
  SkeletonRow,
  SkeletonList,
};
