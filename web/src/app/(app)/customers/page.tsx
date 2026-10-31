"use client";

import { PayerSearch } from "@/components/payer-search";

export default function CustomersPage() {
  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <header>
        <h1 className="text-[1.9rem] leading-tight font-semibold tracking-tight sm:text-[2.2rem]">Customers</h1>
        <p className="text-muted">Everyone who pays you, recognised from their UPI ID, phone or email.</p>
      </header>
      <PayerSearch limit={50} emptyLimit={200} />
    </div>
  );
}
