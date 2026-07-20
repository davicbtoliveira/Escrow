import { describe, expect, it } from "bun:test";
import { approximateAmount, convertMinor, type DisplayExchangeRate, formatMoney } from "./money";

const simulatedRates: DisplayExchangeRate[] = [
  {
    base_currency: "BRL",
    quote_currency: "USD",
    rate_micros: 180_000,
    recorded_at: "2026-01-01T00:00:00Z",
    is_simulated: true,
  },
  {
    base_currency: "USD",
    quote_currency: "BRL",
    rate_micros: 5_400_000,
    recorded_at: "2026-01-01T00:00:00Z",
    is_simulated: true,
  },
];

describe("formatMoney", () => {
  it("formats BRL minor units with the pt-BR locale", () => {
    expect(formatMoney(5_000_000, "BRL")).toContain("50.000,00");
    expect(formatMoney(5_000_000, "BRL")).toContain("R$");
  });

  it("formats USD minor units with the pt-BR locale", () => {
    expect(formatMoney(735_000, "USD")).toContain("7.350,00");
    expect(formatMoney(735_000, "USD")).toContain("US$");
  });
});

describe("convertMinor", () => {
  it("converts minor units with integer micro rates", () => {
    expect(convertMinor(50_000, 180_000)).toBe(9_000);
    expect(convertMinor(735_000, 5_400_000)).toBe(3_969_000);
  });

  it("rounds half up without floating point artifacts", () => {
    expect(convertMinor(25, 180_000)).toBe(5);
    expect(convertMinor(24, 180_000)).toBe(4);
  });
});

describe("approximateAmount", () => {
  it("approximates BRL into USD with the simulated rate", () => {
    expect(approximateAmount(50_000, "BRL", simulatedRates)).toEqual({
      amountMinor: 9_000,
      currency: "USD",
      recordedAt: "2026-01-01T00:00:00Z",
    });
  });

  it("approximates USD into BRL with the simulated rate", () => {
    expect(approximateAmount(735_000, "USD", simulatedRates)).toEqual({
      amountMinor: 3_969_000,
      currency: "BRL",
      recordedAt: "2026-01-01T00:00:00Z",
    });
  });

  it("returns null when the pair has no simulated rate", () => {
    expect(approximateAmount(50_000, "BRL", [])).toBeNull();
  });
});
