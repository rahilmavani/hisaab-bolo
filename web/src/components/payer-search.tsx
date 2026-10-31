"use client";

import { Search, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useState } from "react";
import { useApi } from "@/lib/api";
import type { Payer } from "@/lib/types";
import { Avatar, Badge, Skeleton } from "./ui";

export function useDebounced<T>(value: T, ms = 220): T {
  const [v, setV] = useState(value);
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms);
    return () => clearTimeout(t);
  }, [value, ms]);
  return v;
}

export function PayerRow({ payer }: { payer: Payer }) {
  return (
    <Link
      href={`/customers/${payer.id}`}
      className="flex items-center gap-4 rounded-2xl border border-line bg-white/[0.03] px-4 py-3.5 transition-colors hover:border-line-strong hover:bg-white/[0.06]"
    >
      <Avatar name={payer.display_name} />
      <div className="min-w-0 flex-1">
        <p className="flex min-w-0 items-baseline gap-1.5 text-[1.05rem] font-semibold text-text">
          <span className="max-w-[65%] shrink-0 truncate">{payer.display_name}</span>
          {payer.vpa ? <span className="min-w-0 truncate font-normal text-muted">· {payer.vpa}</span> : null}
        </p>
        <p className="truncate text-[0.95rem] text-muted">{payer.habit}</p>
      </div>
      <div className="flex shrink-0 gap-2">
        {payer.badges.map((b, i) => (
          <span key={b} className={i > 0 ? "hidden sm:inline" : undefined}>
            <Badge label={b} />
          </span>
        ))}
      </div>
    </Link>
  );
}

export function PayerSearchBox({
  value,
  onChange,
  autoFocus,
}: {
  value: string;
  onChange: (v: string) => void;
  autoFocus?: boolean;
}) {
  return (
    <label className="flex items-center gap-3 rounded-full border border-line-strong bg-white/[0.04] px-5 py-3 focus-within:border-sky">
      <Search size={18} className="shrink-0 text-muted" aria-hidden />
      <span className="sr-only">Search customers</span>
      <input
        type="search"
        value={value}
        autoFocus={autoFocus}
        data-autofocus={autoFocus || undefined}
        onChange={(e) => onChange(e.target.value)}
        placeholder="Search by name, UPI ID or phone"
        className="min-w-0 flex-1 bg-transparent text-[1.05rem] text-text outline-none placeholder:text-faint focus-visible:outline-none [&::-webkit-search-cancel-button]:hidden"
      />
      {value ? (
        <button type="button" onClick={() => onChange("")} aria-label="Clear search" className="text-muted hover:text-text">
          <X size={16} />
        </button>
      ) : (
        <span className="hidden text-sm text-faint sm:inline">Customer search</span>
      )}
    </label>
  );
}

/** Search box plus live results. With no query it shows `emptyLimit` top customers. */
export function PayerSearch({ limit = 5, emptyLimit = 0 }: { limit?: number; emptyLimit?: number }) {
  const [q, setQ] = useState("");
  const query = useDebounced(q.trim());
  const show = query.length > 0 || emptyLimit > 0;
  const { data, error, isLoading } = useApi<{ payers: Payer[] }>(
    show ? `/payers?q=${encodeURIComponent(query)}&limit=${query ? limit : emptyLimit}` : null,
    { keepPreviousData: true },
  );

  return (
    <div className="space-y-3">
      <PayerSearchBox value={q} onChange={setQ} />
      {show ? (
        <div className="space-y-2" aria-live="polite">
          {isLoading && !data ? (
            <Skeleton className="h-[74px]" />
          ) : error ? (
            <p className="px-2 text-sm text-rose">Search failed: {error.message}</p>
          ) : data && data.payers.length === 0 ? (
            <p className="px-2 text-sm text-muted">No customer matches “{query}”. Try part of the name, their UPI ID or phone number.</p>
          ) : (
            data?.payers.map((p) => <PayerRow key={p.id} payer={p} />)
          )}
        </div>
      ) : null}
    </div>
  );
}
