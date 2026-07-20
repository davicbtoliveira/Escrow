import type { DisplayExchangeRate } from "../../lib/api";

export type { DisplayExchangeRate };

export type Currency = "BRL" | "USD";

export function formatMoney(amountMinor: number, currency: Currency): string {
  return new Intl.NumberFormat("pt-BR", {
    style: "currency",
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(amountMinor / 100);
}

export function convertMinor(amountMinor: number, rateMicros: number): number {
  return Math.round((amountMinor * rateMicros) / 1_000_000);
}

export function approximateAmount(
  amountMinor: number,
  currency: Currency,
  rates: DisplayExchangeRate[],
): { amountMinor: number; currency: Currency; recordedAt: string } | null {
  const quoteCurrency: Currency = currency === "BRL" ? "USD" : "BRL";
  const rate = rates.find(
    (candidate) =>
      candidate.base_currency === currency &&
      candidate.quote_currency === quoteCurrency &&
      candidate.is_simulated,
  );
  if (!rate) {
    return null;
  }
  return {
    amountMinor: convertMinor(amountMinor, rate.rate_micros),
    currency: quoteCurrency,
    recordedAt: rate.recorded_at,
  };
}
