import { afterEach, describe, expect, it, mock } from "bun:test";
import { checkoutApi } from "./api";

const originalFetch = globalThis.fetch;

afterEach(() => {
  Object.defineProperty(globalThis, "fetch", {
    configurable: true,
    value: originalFetch,
    writable: true,
  });
  mock.restore();
});

describe("checkoutApi.createPixCharge", () => {
  it("uses the public idempotent endpoint and maps only the safe PIX instruction", async () => {
    const fetchMock = mock(() =>
      Promise.resolve(
        new Response(
          JSON.stringify({
            payment: {
              id: "5aef3a70-cfee-4d92-8550-a596c2cc47a6",
              status: "PENDING",
              amount: "500.00",
              currency: "BRL",
              pix_copy_paste: "ESCROW-SANDBOX-PIX:pix_public_reference",
            },
          }),
          { status: 202, headers: { "Content-Type": "application/json" } },
        ),
      ),
    );
    Object.defineProperty(globalThis, "fetch", {
      configurable: true,
      value: fetchMock,
      writable: true,
    });

    const result = await checkoutApi.createPixCharge(
      "chk_public-capability",
      "pix:efb521c6-92a6-4ce5-a512-52c465f12cd9",
    );

    expect(fetchMock).toHaveBeenCalledWith(
      "/api/v1/checkout/chk_public-capability/pix-charges/",
      expect.objectContaining({
        cache: "no-store",
        credentials: "omit",
        headers: expect.objectContaining({
          "Idempotency-Key": "pix:efb521c6-92a6-4ce5-a512-52c465f12cd9",
        }),
        method: "POST",
      }),
    );
    expect(result).toEqual({
      status: "PROCESSING",
      pix: {
        id: "5aef3a70-cfee-4d92-8550-a596c2cc47a6",
        copy_paste: "ESCROW-SANDBOX-PIX:pix_public_reference",
        status: "PENDING",
      },
    });
  });
});
